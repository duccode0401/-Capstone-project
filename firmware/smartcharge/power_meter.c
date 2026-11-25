#include <stdio.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"

/* ===== ADC ===== */
#include "esp_adc/adc_continuous.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

#include "power_meter.h"

/* ===== GPIO & ADC CONFIG ===== */
#define ADC_I_CH           ADC_CHANNEL_6   // GPIO34
#define ADC_V_CH           ADC_CHANNEL_7   // GPIO35
#define ADC_UNIT_USED      ADC_UNIT_1
#define ADC_ATTEN_USED     ADC_ATTEN_DB_12
#define ADC_BITWIDTH_USED  ADC_BITWIDTH_12

/* ===== HE SO CAM BIEN ===== */
#define WCS_SUPPLY_V       3.3f
#define WCS_SENS_5V_V_A    0.072f
#define WCS_SENS_V_A       (WCS_SENS_5V_V_A * (WCS_SUPPLY_V / 5.0f))
#define NUM_TURNS          4.0f  // Assumes the conductor passes through the sensor four times.
#define CURRENT_NOISE_FLOOR_A 0.03f
#define ZMPT_GAIN   170.0f
#define ZMPT_OFFSET_V 1.65f
#define ZMPT_AMPLIFIER_GAIN 3.3f
// Calibrated from the new sensor reading (~5.2 V) against the reference meter
// reading (~218.5 V): 218.5 / 5.2 ~= 42.0.
#define ZMPT_CALIBRATION_FACTOR 42.0f

/* ===== SAMPLING ===== */
#define SAMPLE_FREQ_HZ  20000
#define RMS_SAMPLES    10000

static const char *TAG = "POWER_METER";

/* ===== HANDLE ADC ===== */
static adc_continuous_handle_t adc_handle;
static adc_cali_handle_t adc_cali;

/* ===== OFFSET ZERO ===== */
static float I_ZERO = 0.0f;
static float V_ZERO = 0.0f;

/* ========================================================= */
/* ===================== PRIVATE FUNC ====================== */
/* ========================================================= */

static void adc_continuous_init(void)
{
    adc_continuous_handle_cfg_t handle_cfg = {
        .max_store_buf_size = 4096,
        .conv_frame_size = 256,
    };
    ESP_ERROR_CHECK(adc_continuous_new_handle(&handle_cfg, &adc_handle));

    adc_digi_pattern_config_t pattern[2] = {
        {
            .atten = ADC_ATTEN_USED,
            .channel = ADC_I_CH,
            .unit = ADC_UNIT_USED,
            .bit_width = ADC_BITWIDTH_USED,
        },
        {
            .atten = ADC_ATTEN_USED,
            .channel = ADC_V_CH,
            .unit = ADC_UNIT_USED,
            .bit_width = ADC_BITWIDTH_USED,
        }
    };

    adc_continuous_config_t cfg = {
        .pattern_num = 2,
        .adc_pattern = pattern,
        .sample_freq_hz = SAMPLE_FREQ_HZ,
        .conv_mode = ADC_CONV_SINGLE_UNIT_1,
        .format = ADC_DIGI_OUTPUT_FORMAT_TYPE1,
    };

    ESP_ERROR_CHECK(adc_continuous_config(adc_handle, &cfg));
    ESP_ERROR_CHECK(adc_continuous_start(adc_handle));

    ESP_LOGI(TAG, "ADC continuous started");
}

static void adc_calibration_init(void)
{
    adc_cali_line_fitting_config_t cfg = {
        .unit_id = ADC_UNIT_USED,
        .atten = ADC_ATTEN_USED,
        .bitwidth = ADC_BITWIDTH_USED,
    };
    ESP_ERROR_CHECK(adc_cali_create_scheme_line_fitting(&cfg, &adc_cali));
    ESP_LOGI(TAG, "ADC calibration ready");
}

/* ===== CALIB ZERO ===== */
static esp_err_t adc_calibrate_zero(void)
{
    uint8_t buf[256];
    uint32_t len;
    float sum_i = 0;
    int cnt_i = 0;

    ESP_LOGI(TAG, "CALIB CURRENT ZERO: ENSURE NO CURRENT THROUGH WCS1800");

    int64_t deadline=esp_timer_get_time()+5000000LL;
    while (cnt_i < RMS_SAMPLES)
    {
        if(esp_timer_get_time()>deadline) return ESP_ERR_TIMEOUT;
        if (adc_continuous_read(adc_handle, buf, sizeof(buf), &len, 100) != ESP_OK)
            continue;

        for (int i = 0; i < len; i += SOC_ADC_DIGI_RESULT_BYTES)
        {
            adc_digi_output_data_t *p = (void *)&buf[i];
            int mv;
            if(adc_cali_raw_to_voltage(adc_cali, p->type1.data, &mv)!=ESP_OK) return ESP_FAIL;

            float v = mv / 1000.0f;

            if (p->type1.channel == ADC_I_CH && cnt_i < RMS_SAMPLES)
            {
                sum_i += v;
                cnt_i++;
            }
        }
    }

    I_ZERO = sum_i / cnt_i;
    // LM358 output is biased at 1.65 V.
    V_ZERO = ZMPT_OFFSET_V;

    ESP_LOGI(TAG, "ZERO DONE: I_ZERO=%.4f  V_ZERO=%.4f", I_ZERO, V_ZERO);
    return ESP_OK;
}

/* ===== DO RMS ===== */
static esp_err_t adc_measure_rms(float *irms, float *vrms, float *power)
{
    uint8_t buf[256];
    uint32_t len;
    float sum_i2 = 0, sum_v = 0, sum_v2 = 0;
    int ci = 0, cv = 0;
    float latest_i=0, sum_pair_i=0, sum_pair_v=0, sum_iv=0;
    int pairs=0;
    bool have_i=false;
    int64_t deadline=esp_timer_get_time()+3000000LL;

    while (ci < RMS_SAMPLES || cv < RMS_SAMPLES)
    {
        if(esp_timer_get_time()>deadline) return ESP_ERR_TIMEOUT;
        if (adc_continuous_read(adc_handle, buf, sizeof(buf), &len, 100) != ESP_OK)
            continue;

        for (int i = 0; i < len; i += SOC_ADC_DIGI_RESULT_BYTES)
        {
            adc_digi_output_data_t *p = (void *)&buf[i];
            int mv;
            if(adc_cali_raw_to_voltage(adc_cali, p->type1.data, &mv)!=ESP_OK) return ESP_FAIL;

            float v = mv / 1000.0f;

            if (p->type1.channel == ADC_I_CH && ci < RMS_SAMPLES)
            {
                float i_inst = (v - I_ZERO) / (WCS_SENS_V_A * NUM_TURNS);
                latest_i=i_inst; have_i=true;
                sum_i2 += i_inst * i_inst;
                ci++;
            }
            else if (p->type1.channel == ADC_V_CH && cv < RMS_SAMPLES)
            {
                // Remove the LM358 bias from each ADC sample before RMS.
                float v_adc = mv / 1000.0f;
                float vv = v_adc - V_ZERO;
                if(have_i) {
                    sum_pair_i+=latest_i; sum_pair_v+=vv; sum_iv+=latest_i*vv;
                    pairs++; have_i=false;
                }
                sum_v += vv;
                sum_v2 += vv * vv;
                cv++;
            }
        }
    }

    *irms = sqrtf(sum_i2 / RMS_SAMPLES);
    // Suppress the small apparent current caused by ADC noise near zero.
    if (*irms < CURRENT_NOISE_FLOOR_A) {
        *irms = 0.0f;
    }
    // Remove any residual DC bias error around the nominal 1.65 V offset.
    // Variance computes AC RMS even if the real bias drifts slightly.
    float mean_v = sum_v / RMS_SAMPLES;
    float ac_mean_square = (sum_v2 / RMS_SAMPLES) - (mean_v * mean_v);
    if (ac_mean_square < 0.0f) {
        ac_mean_square = 0.0f;
    }
    float amplifier_output_rms = sqrtf(ac_mean_square);
    *vrms = (amplifier_output_rms / ZMPT_AMPLIFIER_GAIN) *
            ZMPT_GAIN * ZMPT_CALIBRATION_FACTOR;
    if(!pairs) return ESP_ERR_INVALID_STATE;
    /* AC covariance of paired I/V samples removes sensor DC offsets.
     * Adjacent ADC channels have sampling skew: validate phase/gain against a meter. */
    *power=*irms==0?0:fabsf(sum_iv/pairs-(sum_pair_i/pairs)*(sum_pair_v/pairs))*
        (ZMPT_GAIN*ZMPT_CALIBRATION_FACTOR/ZMPT_AMPLIFIER_GAIN);
    return ESP_OK;
}

/* ========================================================= */
/* ===================== PUBLIC API ======================== */
/* ========================================================= */

esp_err_t power_meter_init(void)
{
    adc_calibration_init();
    adc_continuous_init();
    return adc_calibrate_zero();
}

esp_err_t power_meter_get(power_meter_t *meter)
{
    if (!meter) return ESP_ERR_INVALID_ARG;

    return adc_measure_rms(&meter->Irms, &meter->Vrms, &meter->P);
}
