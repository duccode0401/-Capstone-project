#include "power_meter.h"
#include "display_lvgl.h"
#include "wifi_mqtt.h"
#include "sc_core.h"
#include "signed_order.h"
#include "sc_storage.h"
#include "sc_provisioning.h"
#include "sdkconfig.h"
#include <time.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include "driver/gpio.h"
#include "esp_attr.h"
#include "soc/gpio_reg.h"
#include "esp_timer.h"
#include "esp_task_wdt.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_system.h"

#define CHARGE_RELAY_GPIO GPIO_NUM_25
#define CHARGE_TIMEOUT_US (15LL * 60 * 1000000)
/* Business rate intentionally unchanged: 10,000 VND -> 0.9 Wh.
 * Old comment said 130 Wh; business owner must confirm before changing it. */
#define WH_PER_10000_VND 0.9f
static double energy_Wh, energy_target_Wh, lifetime_wh;
static bool charging_enable, healthy, sample_valid;
static int64_t energy_time_us, boot_started_us, charge_deadline_us;
static uint32_t charge_network_epoch;
static float previous_power;
static bool have_previous_power;
static unsigned good_samples;
static bool done_pending;
typedef struct { power_meter_t meter; double total, accumulated; bool charging, valid; } snapshot_t;
static snapshot_t snapshot;
static portMUX_TYPE snapshot_lock=portMUX_INITIALIZER_UNLOCKED;

/* The task watchdog invokes this in ISR context before the panic/reset. */
void IRAM_ATTR esp_task_wdt_isr_user_handler(void)
{
    REG_WRITE(GPIO_OUT_W1TC_REG,1U << CHARGE_RELAY_GPIO);
}
static void relay_set(bool on)
{
    on=on && sc_command_auth_ready() && !provisioning_active();
    ESP_ERROR_CHECK(gpio_set_level(CHARGE_RELAY_GPIO,on?1:0));
    sc_log("INFO",on?"RELAY_ON":"RELAY_OFF",on?"Charging relay ON":"Charging relay OFF");
}
static void payment_rejected(void)
{
    /* All devices on the shared topic see the same orders. Bound public reject logs. */
    static int64_t next_log;
    int64_t now=esp_timer_get_time();
    if(now<next_log) return;
    next_log=now+10000000;
    sc_log("WARN","PAYMENT_REJECTED","Order invalid, replayed or device unavailable");
}
static void handle_payment(const char *msg,uint32_t epoch)
{
    sc_signed_order_t order;
    if(charging_enable || done_pending || !healthy || !sample_valid ||
       !sc_network_ready() || !sc_parse_signed_order(msg,time(NULL),&order)) {
        payment_rejected(); return;
    }
    if(epoch!=sc_network_epoch() || sc_commit_order_sequence(order.sequence)!=ESP_OK ||
       !sc_network_ready() || epoch!=sc_network_epoch()) {
        payment_rejected(); return;
    }
    energy_target_Wh=(order.amount_vnd/10000.0f)*WH_PER_10000_VND;
    energy_Wh=0;
    energy_time_us=esp_timer_get_time(); have_previous_power=false;
    charge_deadline_us=energy_time_us+CHARGE_TIMEOUT_US;
    charge_network_epoch=epoch;
    charging_enable=true;
    relay_set(true);
    display_show_charging((int)order.amount_vnd,energy_target_Wh);
    sc_log("INFO","PAYMENT_ACCEPTED","Authenticated order accepted");
    sc_log("INFO","CHARGING_STARTED","Charging session started");
}
static void stop_charge(const char *reason)
{
    relay_set(false);
    charging_enable=false;
    have_previous_power=false;
    display_show_done();
    sc_log("WARN","CHARGING_STOPPED",reason);
}
static void check_charge_failsafe(void)
{
    if(!charging_enable) return;
    if(!sc_network_ready() || charge_network_epoch!=sc_network_epoch())
        stop_charge("Connection lost; relay OFF");
    else if(esp_timer_get_time()>=charge_deadline_us)
        stop_charge("Session timeout; relay OFF");
}
void update_energy(power_meter_t *m)
{
    int64_t now=esp_timer_get_time();
    if(!charging_enable) { energy_time_us=now; return; }
    double dt=(now-energy_time_us)/1000000.0;
    /* Integrate measured real power using elapsed monotonic time; no 1s assumption.
     * The first post-relay RMS window uses its mean power for the initial interval. */
    double delta=(have_previous_power?(previous_power+m->P)*0.5:m->P)*dt/3600.0;
    if(delta>=0 && isfinite(delta)) { energy_Wh+=delta; lifetime_wh+=delta; }
    energy_time_us=now; previous_power=m->P; have_previous_power=true;
    display_update_energy(energy_Wh,energy_target_Wh);
    if(energy_Wh>=energy_target_Wh) {
        relay_set(false); charging_enable=false;
        display_show_done(); done_pending=true;
        sc_log("INFO","CHARGE_COMPLETED","Relay OFF, charging completed");
    }
}
static void publish_done(void)
{
    if(!done_pending) return;
    char json[160];
    snprintf(json,sizeof(json),"{\"device\":\"%s\",\"port\":1,\"status\":\"DONE\"}",sc_config.device);
    if(sc_publish(SC_BASE "status",json,true)) done_pending=false;
}
static void telemetry_task(void *arg)
{
    TickType_t wake=xTaskGetTickCount();
    int64_t previous_us=esp_timer_get_time();
    double previous_wh=0;
    while(1) {
        vTaskDelayUntil(&wake,pdMS_TO_TICKS(10000));
        snapshot_t s;
        portENTER_CRITICAL(&snapshot_lock); s=snapshot; portEXIT_CRITICAL(&snapshot_lock);
        int64_t now=esp_timer_get_time();
        double interval=(now-previous_us)/1000000.0;
        char json[640];
        snprintf(json,sizeof(json),
        "{\"device\":\"%s\",\"port\":1,\"voltage_v\":%.3f,\"current_a\":%.4f,"
        "\"power_w\":%.3f,\"energy_total_wh\":%.6f,\"energy_delta_wh\":%.6f,"
        "\"charging\":%s,\"status\":\"%s\",\"interval_s\":%.3f}",
        sc_config.device,(double)s.meter.Vrms,(double)s.meter.Irms,(double)s.meter.P,
        s.total,s.accumulated-previous_wh,s.charging?"true":"false",
        s.charging?"CHARGING":"IDLE",interval);
        if(s.valid) sc_publish(SC_BASE "telemetry",json,false);
        previous_wh=s.accumulated; previous_us=now;
        sc_memory_log();
    }
}
static void confirm_startup(void)
{
    if(healthy || good_samples<5 || esp_timer_get_time()-boot_started_us<30000000LL ||
       !display_is_healthy()) return;
    healthy=true;
    sc_log("INFO","HEALTHY","Application startup health checks passed");
}
void app_main(void)
{
    /* Configure OFF before LVGL, NVS, ADC or any network initialization. */
    sc_relay_off();
    ESP_ERROR_CHECK(storage_init());
    ESP_ERROR_CHECK(sc_init());
#ifdef CONFIG_HEADLESS_TEST_MODE
    sc_network_start_headless();
    sc_log("INFO","HEALTHY","Headless application running with relay disabled");
    return;
#endif
    ESP_ERROR_CHECK(display_lvgl_init());
    ESP_ERROR_CHECK(power_meter_init());
    wifi_init_sta();
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    power_meter_t meter={0};
    boot_started_us=esp_timer_get_time();
    ESP_ERROR_CHECK(xTaskCreate(telemetry_task,"telemetry",4096,NULL,2,NULL)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
    sc_memory_log();
    int64_t last_memory_log=esp_timer_get_time();
    while(1) {
        check_charge_failsafe();
        esp_err_t e=power_meter_get(&meter);
        check_charge_failsafe();
        sample_valid=e==ESP_OK && isfinite(meter.P) && isfinite(meter.Irms) && isfinite(meter.Vrms);
        if(!sample_valid) {
            if(charging_enable) stop_charge("Sensor invalid; relay OFF");
            good_samples=0;
            have_previous_power=false;
            static int64_t next_sensor_log;
            int64_t now=esp_timer_get_time();
            if(now>=next_sensor_log) {
                sc_log("ERROR","SENSOR_ERROR","ADC measurement failed; relay OFF");
                next_sensor_log=now+10000000;
            }
        } else {
            good_samples++;
            update_energy(&meter);
            display_update_power(meter.Irms,meter.Vrms,meter.P);
        }
        sc_set_health(sample_valid,display_is_healthy());
        confirm_startup(); publish_done();
        sc_command_t command;
        /* Bound work per measurement iteration; all session/relay state has one owner. */
        if(sc_receive(&command)) {
            handle_payment(command.data,command.network_epoch);
        }
        portENTER_CRITICAL(&snapshot_lock);
        snapshot=(snapshot_t){.meter=meter,.total=energy_Wh,
                              .accumulated=lifetime_wh,
                              .charging=charging_enable,.valid=sample_valid};
        portEXIT_CRITICAL(&snapshot_lock);
        ESP_ERROR_CHECK(esp_task_wdt_reset());
        if(esp_timer_get_time()-last_memory_log>=10000000LL) {
            sc_memory_log(); last_memory_log=esp_timer_get_time();
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}
