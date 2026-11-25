#include "sc_core.h"
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#include <time.h>
#include "cJSON.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_crt_bundle.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_random.h"
#include "mqtt_client.h"

typedef struct { char topic[80], json[768]; int64_t queued_at; bool reliable; } outbound_t;
#define TELEMETRY_MAX_AGE_US 20000000LL
#define EVENT_MAX_AGE_US 120000000LL
static QueueHandle_t tx_queue,rx_queue;
static esp_mqtt_client_handle_t client;
static EventGroupHandle_t network;
static atomic_uint dropped,connection_epoch;
static char mqtt_client_id[32];
static uint32_t incoming_epoch;
static void (*wifi_cb)(bool);
static char incoming[SC_MESSAGE_MAX];
static int incoming_total,incoming_used;
static bool incoming_ota,incoming_valid;
#define WIFI_UP BIT0
#define MQTT_UP BIT1

uint32_t sc_dropped_messages(void) { return atomic_load(&dropped); }
uint32_t sc_network_epoch(void) { return atomic_load(&connection_epoch); }
bool sc_network_ready(void)
{
    return network && (xEventGroupGetBits(network)&(WIFI_UP|MQTT_UP))==(WIFI_UP|MQTT_UP);
}
bool sc_download_ready(void)
{
    return !sc_config.mqtt_public_test_mode && network && (xEventGroupGetBits(network)&WIFI_UP) && time(NULL)>1735689600;
}
bool sc_receive(sc_command_t *cmd)
{
    return !sc_config.mqtt_public_test_mode && sc_network_ready() &&
        xQueueReceive(rx_queue,cmd,0)==pdTRUE && cmd->network_epoch==sc_network_epoch() &&
        sc_network_ready();
}
bool sc_publish(const char *topic,const char *json,bool reliable)
{
    outbound_t m={.reliable=reliable,.queued_at=esp_timer_get_time()};
    if(!tx_queue || strlen(json)>=sizeof(m.json)) return false;
    if(sc_config.mqtt_public_test_mode) {
        const char *kind;
        if(!strcmp(topic,SC_BASE "telemetry")) kind="telemetry";
        else if(!strcmp(topic,SC_BASE "logs")) kind="logs";
        else return false;
        snprintf(m.topic,sizeof(m.topic),SC_PUBLIC_TEST_BASE "%s/%s",sc_config.device,kind);
    } else {
        if(strlen(topic)>=sizeof(m.topic)) return false;
        strcpy(m.topic,topic);
    }
    if(!strcmp(topic,SC_BASE "telemetry") && !sc_network_ready()) return false;
    strcpy(m.json,json);
    if(xQueueSend(tx_queue,&m,0)!=pdTRUE) { atomic_fetch_add(&dropped,1); return false; }
    return true;
}
void sc_log(const char *level,const char *event,const char *message)
{
    /* Only fixed application events; never forward arbitrary ESP_LOG or command payloads. */
    cJSON *j=cJSON_CreateObject(); if(!j) return;
    cJSON_AddStringToObject(j,"device",sc_config.device);
    cJSON_AddStringToObject(j,"level",level);
    cJSON_AddStringToObject(j,"event",event);
    if(!sc_config.mqtt_public_test_mode) cJSON_AddStringToObject(j,"message",message);
    cJSON_AddNumberToObject(j,"uptime_ms",esp_timer_get_time()/1000);
    char json[512];
    if(cJSON_PrintPreallocated(j,json,sizeof(json),false)) sc_publish(SC_BASE "logs",json,false);
    cJSON_Delete(j);
}
void sc_status(const sc_request_t *r,const char *status,int progress)
{
    sc_status_error(r,status,progress,NULL);
}
void sc_status_error(const sc_request_t *r,const char *status,int progress,const char *error)
{
    cJSON *j=cJSON_CreateObject(); if(!j) return;
    cJSON_AddStringToObject(j,"device",sc_config.device);
    cJSON_AddStringToObject(j,"request_id",r?r->request_id:"");
    cJSON_AddStringToObject(j,"version",r?r->version:"");
    cJSON_AddStringToObject(j,"status",status);
    cJSON_AddNumberToObject(j,"progress",progress);
    if(error) cJSON_AddStringToObject(j,"error",error);
    char json[512];
    if(cJSON_PrintPreallocated(j,json,sizeof(json),false)) sc_publish(SC_BASE "ota/status",json,true);
    cJSON_Delete(j);
}
static bool topic_is(esp_mqtt_event_handle_t e,const char *topic)
{
    return e->topic_len==(int)strlen(topic) && !memcmp(e->topic,topic,e->topic_len);
}
static void mqtt_error(const esp_mqtt_error_codes_t *error)
{
    if(!error) return;
    char reason[128];
    if(error->error_type==MQTT_ERROR_TYPE_TCP_TRANSPORT) {
        snprintf(reason,sizeof(reason),"Transport error tls=%d stack=%d cert_flags=%x errno=%d",
                 error->esp_tls_last_esp_err,error->esp_tls_stack_err,
                 (unsigned)error->esp_tls_cert_verify_flags,error->esp_transport_sock_errno);
    } else if(error->error_type==MQTT_ERROR_TYPE_CONNECTION_REFUSED) {
        snprintf(reason,sizeof(reason),"Broker refused connection code=%d",error->connect_return_code);
    } else {
        snprintf(reason,sizeof(reason),"MQTT error type=%d",error->error_type);
    }
    ESP_LOGW("SC_MQTT","%s",reason);
    sc_log("WARN","MQTT_ERROR",reason);
}
static void mqtt_event(void *arg,esp_event_base_t base,int32_t id,void *data)
{
    esp_mqtt_event_handle_t e=data;
    if(id==MQTT_EVENT_ERROR) {
        mqtt_error(e->error_handle);
    } else if(id==MQTT_EVENT_CONNECTED) {
        xEventGroupSetBits(network,MQTT_UP);
#ifndef SC_RECOVERY
        if(sc_ota_enabled() && esp_mqtt_client_subscribe(client,SC_BASE "orders",1)<0)
            sc_log("WARN","MQTT_ERROR","Order subscription enqueue failed");
#endif
        if(sc_ota_enabled() && esp_mqtt_client_subscribe(client,SC_BASE "ota/command",1)<0)
            sc_log("WARN","MQTT_ERROR","OTA subscription enqueue failed");
        sc_log("INFO","MQTT_CONNECTED","MQTT session connected");
    } else if(id==MQTT_EVENT_DISCONNECTED) {
        xEventGroupClearBits(network,MQTT_UP);
        atomic_fetch_add(&connection_epoch,1);
        xQueueReset(rx_queue);
        incoming_valid=false;
        sc_log("WARN","MQTT_DISCONNECTED","MQTT session disconnected");
    } else if(id==MQTT_EVENT_DATA && sc_network_ready() && sc_ota_enabled()) {
        if(e->current_data_offset==0) {
            incoming_used=0; incoming_total=e->total_data_len;
            incoming_epoch=sc_network_epoch();
            incoming_ota=topic_is(e,SC_BASE "ota/command");
            bool supported=incoming_ota;
#ifndef SC_RECOVERY
            supported=supported || topic_is(e,SC_BASE "orders");
#endif
            incoming_valid=supported && !e->retain && incoming_total>0 &&
                incoming_total<sizeof(incoming);
        }
        if(!incoming_valid || incoming_epoch!=sc_network_epoch() ||
           e->current_data_offset!=incoming_used || e->data_len<0 ||
           e->data_len>incoming_total-incoming_used) { incoming_valid=false; return; }
        memcpy(incoming+incoming_used,e->data,e->data_len); incoming_used+=e->data_len;
        if(incoming_used==incoming_total) {
            incoming[incoming_used]=0;
            sc_command_t cmd={.ota=incoming_ota,.network_epoch=incoming_epoch};
            if(memchr(incoming,0,incoming_used)) { incoming_valid=false; return; }
            memcpy(cmd.data,incoming,incoming_used+1);
            if(xQueueSend(rx_queue,&cmd,0)!=pdTRUE) atomic_fetch_add(&dropped,1);
            incoming_valid=false;
        }
    }
}
static void wifi_event(void *arg,esp_event_base_t base,int32_t id,void *data)
{
    if(base==WIFI_EVENT && id==WIFI_EVENT_STA_START) esp_wifi_connect();
    else if(base==WIFI_EVENT && id==WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(network,WIFI_UP|MQTT_UP);
        atomic_fetch_add(&connection_epoch,1);
        if(wifi_cb) wifi_cb(false);
        sc_log("WARN","WIFI_DISCONNECTED","Wi-Fi disconnected; reconnecting");
        /* esp_wifi_connect is asynchronous; bounded driver retry interval. */
        esp_wifi_connect();
    } else if(base==IP_EVENT && id==IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(network,WIFI_UP);
        if(wifi_cb) wifi_cb(true);
        sc_log("INFO","WIFI_CONNECTED","Wi-Fi obtained IP address");
    }
}
static void network_worker(void *arg)
{
    outbound_t m;
    bool mqtt_started=false;
    int64_t last_diag=0,next_start=0;
    while(1) {
        /* TLS certificate validity checks need wall time; SNTP supplies time, not OTA authority. */
        int64_t now=esp_timer_get_time();
        if(!mqtt_started && now>=next_start && (xEventGroupGetBits(network)&WIFI_UP) && time(NULL)>1735689600) {
            mqtt_started=esp_mqtt_client_start(client)==ESP_OK;
            next_start=now+5000000;
            if(!mqtt_started) sc_log("WARN","MQTT_ERROR","Failed to start MQTT task");
        }
        if(xQueuePeek(tx_queue,&m,0)==pdTRUE) {
            int64_t max_age=strstr(m.topic,"/telemetry")?TELEMETRY_MAX_AGE_US:EVENT_MAX_AGE_US;
            if(now-m.queued_at>=max_age) {
                xQueueReceive(tx_queue,&m,0);
                atomic_fetch_add(&dropped,1);
            } else if(sc_network_ready() && esp_mqtt_client_get_outbox_size(client)<2048 &&
                      esp_mqtt_client_enqueue(client,m.topic,m.json,0,m.reliable?1:0,0,true)>=0) {
                xQueueReceive(tx_queue,&m,0);
            }
        }
        if(now-last_diag>=10000000LL) {
            sc_memory_log();
            if(sc_network_ready() && !sc_config.mqtt_public_test_mode) {
                wifi_ap_record_t ap;
                int rssi=esp_wifi_sta_get_ap_info(&ap)==ESP_OK?ap.rssi:0;
                char diagnostics[128];
                snprintf(diagnostics,sizeof(diagnostics),"heap=%u B rssi=%d dBm firmware=%.31s",
                         (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
                         rssi,esp_app_get_description()->version);
                sc_log("INFO","HEALTH_DIAGNOSTICS",diagnostics);
            }
            last_diag=now;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
void sc_wait_outbox(unsigned timeout_ms)
{
    int64_t until=esp_timer_get_time()+(int64_t)timeout_ms*1000;
    while(esp_timer_get_time()<until) {
        if(tx_queue && uxQueueMessagesWaiting(tx_queue)==0 && client && esp_mqtt_client_get_outbox_size(client)==0) break;
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
void sc_network_start(void (*cb)(bool))
{
    wifi_cb=cb;
    tx_queue=xQueueCreate(8,sizeof(outbound_t)); rx_queue=xQueueCreate(3,sizeof(sc_command_t));
    network=xEventGroupCreate();
    ESP_ERROR_CHECK(tx_queue && rx_queue && network?ESP_OK:ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_netif_init()); ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_create_default_wifi_sta()?ESP_OK:ESP_ERR_NO_MEM);
    wifi_init_config_t init=WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,wifi_event,NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,wifi_event,NULL));
    wifi_config_t wifi={0};
    memcpy(wifi.sta.ssid,sc_config.ssid,strlen(sc_config.ssid));
    memcpy(wifi.sta.password,sc_config.password,strlen(sc_config.password));
    wifi.sta.threshold.authmode=WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA,&wifi));
    esp_sntp_config_t sntp=ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    ESP_ERROR_CHECK(esp_netif_sntp_init(&sntp));
    bool authenticated=!sc_config.mqtt_public_test_mode && !sc_config.mqtt_public_signed_mode;
    if(sc_config.mqtt_public_signed_mode)
        snprintf(mqtt_client_id,sizeof(mqtt_client_id),"sc-%08lx",(unsigned long)esp_random());
    esp_mqtt_client_config_t mqtt={
        .broker.address.uri=sc_config.broker,
        .broker.verification.crt_bundle_attach=esp_crt_bundle_attach,
        .credentials.username=authenticated && sc_config.mqtt_user[0]?sc_config.mqtt_user:NULL,
        .credentials.authentication.password=authenticated && sc_config.mqtt_password[0]?sc_config.mqtt_password:NULL,
        .credentials.client_id=sc_config.mqtt_public_signed_mode?mqtt_client_id:sc_config.device,
        .session.keepalive=15,
        .buffer.size=1024, .buffer.out_size=1024,
        .task.stack_size=6144, .outbox.limit=4096,
        .network.timeout_ms=5000, .network.reconnect_timeout_ms=5000,
    };
    client=esp_mqtt_client_init(&mqtt);
    ESP_ERROR_CHECK(client?ESP_OK:ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(client,ESP_EVENT_ANY_ID,mqtt_event,NULL));
    ESP_ERROR_CHECK(xTaskCreate(network_worker,"sc_network",4096,NULL,3,NULL)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_wifi_start());
    sc_log("INFO","RELAY_OFF","Relay OFF at startup");
    char reason[64]; snprintf(reason,sizeof(reason),"Reset reason=%d",esp_reset_reason());
    esp_reset_reason_t reset=esp_reset_reason();
    bool watchdog=reset==ESP_RST_TASK_WDT || reset==ESP_RST_INT_WDT || reset==ESP_RST_WDT;
    sc_log(watchdog?"WARN":"INFO",watchdog?"WATCHDOG_RESET":"REBOOT_REASON",reason);
}
