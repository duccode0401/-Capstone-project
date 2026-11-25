#include "power_meter.h"
#include "display_lvgl.h"
#include "wifi_mqtt.h"
#include "sc_core.h"
#include <stdio.h>
#include <string.h>
#include <math.h>
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_task_wdt.h"
#include "esp_ota_ops.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_system.h"
#include "sdkconfig.h"

#define CHARGE_RELAY_GPIO GPIO_NUM_25
/* Business rate intentionally unchanged: 10,000 VND -> 0.9 Wh.
 * Old comment said 130 Wh; business owner must confirm before changing it. */
#define WH_PER_10000_VND 0.9f
static double energy_Wh, energy_target_Wh, lifetime_wh;
static bool charging_enable, ota_locked, healthy, sample_valid;
static int64_t energy_time_us, boot_started_us;
static float previous_power;
static bool have_previous_power;
static unsigned good_samples;
static bool done_pending;
static bool boot_candidate;
static esp_timer_handle_t health_deadline;
typedef struct { power_meter_t meter; double total, accumulated; bool charging, valid; } snapshot_t;
static snapshot_t snapshot;
static portMUX_TYPE snapshot_lock=portMUX_INITIALIZER_UNLOCKED;

static void health_timeout(void *arg)
{
    /* PENDING_VERIFY is rejected by the standard rollback bootloader next boot. */
    sc_relay_off();
    esp_restart();
}
static void relay_set(bool on)
{
    ESP_ERROR_CHECK(gpio_set_level(CHARGE_RELAY_GPIO,on?1:0));
    sc_log("INFO",on?"RELAY_ON":"RELAY_OFF",on?"Charging relay ON":"Charging relay OFF");
}
void handle_payment(const char *msg)
{
    char dev[32],extra;
    int port,money;
    if(sscanf(msg,"%31s %d %d %c",dev,&port,&money,&extra)!=3 || money<=0 ||
       strcmp(dev,sc_config.device) || port!=1) return;
    sc_log("INFO","PAYMENT_RECEIVED","Payment command received for this device");
    if(charging_enable || ota_locked || !healthy || !sample_valid) {
        sc_log("WARN","PAYMENT_REJECTED","Device busy or not ready"); return;
    }
    energy_target_Wh=(money/10000.0f)*WH_PER_10000_VND;
    energy_Wh=0;
    energy_time_us=esp_timer_get_time(); have_previous_power=false;
    charging_enable=true;
    display_show_charging(money,energy_target_Wh);
    relay_set(true);
    sc_log("INFO","CHARGING_STARTED","Charging session started");
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
static void ota_command(const char *json)
{
    sc_request_t r;
    esp_err_t e=sc_parse_request(json,&r);
    if(e!=ESP_OK) { sc_log("WARN","OTA_REJECTED","Invalid, unauthorized or disabled OTA command"); return; }
    /* QoS1 duplicates must not turn an already accepted backend job into FAILED/BUSY. */
    e=sc_check_request(&r);
    if(e!=ESP_OK) { sc_log("WARN","OTA_REJECTED","Replay, duplicate or incompatible version ignored"); return; }
    if(charging_enable || gpio_get_level(CHARGE_RELAY_GPIO) || ota_locked || !healthy) {
        sc_status(&r,"REJECTED_BUSY",0);
        sc_log("WARN","OTA_REJECTED","Charging or startup active"); return;
    }
    e=sc_accept_request(&r);
    if(e!=ESP_OK) {
        sc_status(&r,"FAILED",0); sc_log("WARN","OTA_REJECTED","Replay, version or storage check failed"); return;
    }
    ota_locked=true; relay_set(false);
    sc_status(&r,"ACCEPTED",0); sc_log("INFO","OTA_ACCEPTED","OTA request persisted");
    sc_wait_outbox(2000);
    e=sc_go_recovery();
    if(e==ESP_OK) { sc_status(&r,"REBOOTING",0); sc_wait_outbox(1000); esp_restart(); }
    sc_status(&r,"FAILED",0); sc_log("ERROR","OTA_FAILED","Recovery selection failed; relay remains OFF");
    /* Keep payments locked: pending request must be resolved by operator/reset. */
}
static void confirm_startup(void)
{
    if(healthy || good_samples<5 || esp_timer_get_time()-boot_started_us<30000000LL ||
       (boot_candidate && !sc_network_ready()) || !display_is_healthy()) return;
    ESP_ERROR_CHECK(sc_confirm_healthy());
    healthy=true;
    ESP_ERROR_CHECK(esp_timer_stop(health_deadline));
    sc_log("INFO","HEALTHY","Application startup health checks passed");
}
static void report_version(void)
{
    static int64_t last;
    if(!healthy || !sc_network_ready() || esp_timer_get_time()-last<10000000LL) return;
    sc_request_t r; char state[24]={0};
    if(sc_load_request(&r)==ESP_OK && sc_get_state(state,sizeof(state))==ESP_OK &&
       (!strcmp(state,"candidate") || !strcmp(state,"success"))) {
        if(!strcmp(r.version,esp_app_get_description()->version)) {
            if(!strcmp(state,"candidate")) {
                ESP_ERROR_CHECK(sc_set_state("success"));
                sc_log("INFO","OTA_SUCCESS","New firmware booted and passed health checks");
            }
            sc_status(&r,"SUCCESS",100);
        }
    }
    last=esp_timer_get_time();
}
void app_main(void)
{
    /* Configure OFF before LVGL, NVS, ADC or any network initialization. */
    sc_relay_off();
    ESP_ERROR_CHECK(sc_init(CONFIG_ESP_WIFI_SSID,CONFIG_ESP_WIFI_PASSWORD));
    ESP_ERROR_CHECK(sc_boot_guard());
    esp_ota_img_states_t initial_state;
    const esp_partition_t *running=esp_ota_get_running_partition();
    boot_candidate=running && esp_ota_get_state_partition(running,&initial_state)==ESP_OK &&
                   initial_state==ESP_OTA_IMG_PENDING_VERIFY;
    const esp_timer_create_args_t guard={.callback=health_timeout,.name="boot_health"};
    ESP_ERROR_CHECK(esp_timer_create(&guard,&health_deadline));
    ESP_ERROR_CHECK(esp_timer_start_once(health_deadline,90000000LL));
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
        esp_err_t e=power_meter_get(&meter);
        sample_valid=e==ESP_OK && isfinite(meter.P) && isfinite(meter.Irms) && isfinite(meter.Vrms);
        if(!sample_valid) {
            relay_set(false); charging_enable=false; good_samples=0;
            sc_log("ERROR","SENSOR_ERROR","ADC measurement failed; relay OFF");
        } else {
            good_samples++;
            update_energy(&meter);
            display_update_power(meter.Irms,meter.Vrms,meter.P);
        }
        confirm_startup(); report_version(); publish_done();
        sc_command_t command;
        /* Bound work per measurement iteration; all session/relay state has one owner. */
        if(sc_receive(&command)) {
            if(command.ota) ota_command(command.data);
            else handle_payment(command.data);
        }
        portENTER_CRITICAL(&snapshot_lock);
        snapshot=(snapshot_t){.meter=meter,.total=energy_Wh,.accumulated=lifetime_wh,
                              .charging=charging_enable,.valid=sample_valid};
        portEXIT_CRITICAL(&snapshot_lock);
        ESP_ERROR_CHECK(esp_task_wdt_reset());
        if(esp_timer_get_time()-last_memory_log>=10000000LL) {
            sc_memory_log(); last_memory_log=esp_timer_get_time();
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}
