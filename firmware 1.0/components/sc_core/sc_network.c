#include "sc_core.h"
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#include <time.h>
#include <inttypes.h>
#include <stdlib.h>
#include "driver/uart.h"
#include "driver/gpio.h"
#include "esp_netif_ip_addr.h"
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
#include "sc_provisioning.h"
#include "sc_storage.h"

#define SC_ACK_SLOTS 3
#define SC_ACK_TIMEOUT_US 5000000LL
#define SC_ACK_MAX_ATTEMPTS 4
#define SC_ACK_LIFETIME_US 90000000LL
#define SC_ACK_ID_LEN 18

typedef struct {
    char topic[80], json[768], id[SC_ACK_ID_LEN];
    const char *kind;
    int64_t queued_at;
    bool wait_for_transport, state_changing;
} outbound_t;
typedef struct {
    outbound_t message;
    int packet;
    int64_t sent_at, due;
    unsigned attempts;
    bool active, broker_ack, server_terminal, transport_done, retry_requested;
} pending_t;
typedef struct {
    char id[SC_ACK_ID_LEN], kind[16], error[33];
    int packet;
    bool from_server, success, retryable;
} ack_event_t;
static QueueHandle_t tx_queue,rx_queue,ack_queue;
static pending_t pending[SC_ACK_SLOTS];
static uint32_t nonce[2];
typedef enum { SERIAL_NORMAL, SERIAL_DEBUG, SERIAL_ERROR } serial_mode_t;
static atomic_uint serial_mode;
static bool serial_rx_ready;
static unsigned stats_tx,stats_ok,stats_fail,stats_retry;
static atomic_uint tx_sequence, transport_wait;
static atomic_uint health;
static char ack_topic[80];
static char mqtt_base[64];
static bool headless;
static bool server_v2_seen;
static atomic_bool ack_subscription_ready;
static int ack_subscription_id=-1;
static unsigned boot_event_index;
static esp_mqtt_client_handle_t client;
static EventGroupHandle_t network;
static atomic_uint dropped,connection_epoch;
static char mqtt_client_id[32];
static uint32_t incoming_epoch;
static void (*wifi_cb)(bool);
static char incoming[SC_MESSAGE_MAX];
static int incoming_total,incoming_used;
static bool incoming_valid;
#define WIFI_UP BIT0
#define MQTT_UP BIT1
#define WIFI_LOG_UP BIT2
#define WIFI_LOG_DOWN BIT3

bool sc_serial_debug(void) { return atomic_load(&serial_mode)==SERIAL_DEBUG; }
uint32_t sc_dropped_messages(void) { return atomic_load(&dropped); }
uint32_t sc_network_epoch(void) { return atomic_load(&connection_epoch); }
bool sc_network_ready(void)
{
    return network && (xEventGroupGetBits(network)&(WIFI_UP|MQTT_UP))==(WIFI_UP|MQTT_UP);
}
bool sc_receive(sc_command_t *cmd)
{
    return sc_command_auth_ready() && sc_network_ready() &&
        xQueueReceive(rx_queue,cmd,0)==pdTRUE && cmd->network_epoch==sc_network_epoch() &&
        sc_network_ready();
}
void sc_set_health(bool sensor_ok,bool display_ok)
{
    atomic_store(&health,(sensor_ok?1u:0u)|(display_ok?2u:0u));
}

static bool prepare_message(outbound_t *m,const char *topic,const char *json,bool reliable)
{
    if(!m || json[0]!='{') return false;
    *m=(outbound_t){.queued_at=esp_timer_get_time(),.wait_for_transport=reliable};
    if(!strcmp(topic,SC_BASE "telemetry")) m->kind="telemetry";
    else if(!strcmp(topic,SC_BASE "logs")) m->kind="logs";
    else if(!strcmp(topic,SC_BASE "heartbeat")) m->kind="heartbeat";
    else if(!strcmp(topic,SC_BASE "status")) m->kind="status";
    else return false;
    m->state_changing=reliable;

    snprintf(m->topic,sizeof(m->topic),"%s%s",mqtt_base,m->kind);
    unsigned last=atomic_load(&tx_sequence);
    do {
        if(last==UINT32_MAX) return false;
    } while(!atomic_compare_exchange_weak(&tx_sequence,&last,last+1));
    uint32_t seq=last+1;
    snprintf(m->id,sizeof(m->id),"%08"PRIx32"-%08"PRIx32,nonce[0],seq);
    int n=snprintf(m->json,sizeof(m->json),"{\"protocol_version\":2,\"device\":\"%s\","
        "\"message_id\":\"%s\",\"message_type\":\"%s\",\"uptime_ms\":%"PRId64","
        "\"logical_device\":\"%s\",%s",
        sc_routing_device(),m->id,m->kind,m->queued_at/1000,sc_logical_device(),json+1);
    if(n<0 || n>=sizeof(m->json)) {
        ESP_LOGW("TX","FAILED_PAYLOAD_TOO_LARGE id=%s type=%s",m->id,m->kind);
        return false;
    }
    return true;
}
bool sc_publish(const char *topic,const char *json,bool reliable)
{
    if(!tx_queue) return false;
    outbound_t m;
    if(!prepare_message(&m,topic,json,reliable)) return false;
    if(reliable) atomic_fetch_add(&transport_wait,1);
    BaseType_t queued=xQueueSend(tx_queue,&m,0);
    if(queued!=pdTRUE) {
        if(reliable) atomic_fetch_sub(&transport_wait,1);
        atomic_fetch_add(&dropped,1);
        ESP_LOGW("TX","FAILED_QUEUE_FULL id=%s topic=%s",m.id,m.topic);
        return false;
    }
    if(sc_serial_debug()) ESP_LOGI("TX","QUEUED id=%s device=%s type=%s topic=%s%s",
             m.id,sc_routing_device(),m.kind,m.topic,sc_network_ready()?"":" offline");
    return true;
}
static bool prepare_log(outbound_t *out,const char *level,const char *event,const char *message)
{
    /* Only fixed application events; never forward arbitrary ESP_LOG or command payloads. */
    cJSON *j=cJSON_CreateObject(); if(!j) return false;
    wifi_ap_record_t ap;
    int rssi=esp_wifi_sta_get_ap_info(&ap)==ESP_OK?ap.rssi:0;
    cJSON_AddStringToObject(j,"level",!strcmp(level,"WARN")?"WARNING":level);
    cJSON_AddStringToObject(j,"event",event);
    char bounded[161];
    snprintf(bounded,sizeof(bounded),"%.160s",message?message:"");
    cJSON_AddStringToObject(j,"message",bounded);
    cJSON_AddStringToObject(j,"firmware_version",esp_app_get_description()->version);
    cJSON_AddNumberToObject(j,"uptime_ms",esp_timer_get_time()/1000);
    cJSON_AddNumberToObject(j,"reset_reason",esp_reset_reason());
    cJSON_AddNumberToObject(j,"free_heap",esp_get_free_heap_size());
    cJSON_AddNumberToObject(j,"min_free_heap",esp_get_minimum_free_heap_size());
    cJSON_AddNumberToObject(j,"rssi",rssi);
    cJSON_AddBoolToObject(j,"mqtt_connected",sc_network_ready());
    char json[768];
    bool prepared=cJSON_PrintPreallocated(j,json,sizeof(json),false) &&
        prepare_message(out,SC_BASE "logs",json,false);
    cJSON_Delete(j);
    return prepared;
}
void sc_log(const char *level,const char *event,const char *message)
{
    outbound_t message_out;
    if(!tx_queue || !prepare_log(&message_out,level,event,message)) return;
    if(xQueueSend(tx_queue,&message_out,0)!=pdTRUE) {
        atomic_fetch_add(&dropped,1);
        ESP_LOGW("TX","FAILED_QUEUE_FULL id=%s topic=%s",message_out.id,message_out.topic);
    } else if(sc_serial_debug()) {
        ESP_LOGI("TX","QUEUED id=%s device=%s type=%s topic=%s%s",message_out.id,
                 sc_routing_device(),message_out.kind,message_out.topic,
                 sc_network_ready()?"":" offline");
    }
}
static bool topic_is(esp_mqtt_event_handle_t e,const char *topic)
{
    return e->topic_len==(int)strlen(topic) && !memcmp(e->topic,topic,e->topic_len);
}
static bool safe_error_code(const char *s)
{
    return *s && strlen(s)<=32 && strspn(s,"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")==strlen(s);
}
static void receive_ack(esp_mqtt_event_handle_t e)
{
    if(e->retain || e->current_data_offset || e->data_len<=0 || e->data_len!=e->total_data_len ||
       e->data_len>=384 || memchr(e->data,0,e->data_len) || memchr(e->data,'\\',e->data_len)) return;
    char payload[384];
    memcpy(payload,e->data,e->data_len);
    payload[e->data_len]=0;
    cJSON *j=sc_parse_flat_json(payload,sizeof(payload)-1);
    if(!j) return;
    const cJSON *device=cJSON_GetObjectItemCaseSensitive(j,"device");
    const cJSON *id=cJSON_GetObjectItemCaseSensitive(j,"message_id");
    const cJSON *kind=cJSON_GetObjectItemCaseSensitive(j,"message_type");
    const cJSON *status=cJSON_GetObjectItemCaseSensitive(j,"status");
    const cJSON *version=cJSON_GetObjectItemCaseSensitive(j,"protocol_version");
    const cJSON *retry=cJSON_GetObjectItemCaseSensitive(j,"retryable");
    const cJSON *code=cJSON_GetObjectItemCaseSensitive(j,"error_code");
    const cJSON *timestamp=cJSON_GetObjectItemCaseSensitive(j,"server_timestamp");
    bool success=cJSON_IsString(status) && !strcmp(status->valuestring,"SUCCESS");
    bool failed=cJSON_IsString(status) && !strcmp(status->valuestring,"FAILED");
    if(cJSON_IsString(device) && !strcmp(device->valuestring,sc_routing_device()) &&
       cJSON_IsString(id) && strlen(id->valuestring)==SC_ACK_ID_LEN-1 &&
       id->valuestring[8]=='-' && strspn(id->valuestring,"0123456789abcdef")==8 &&
       strspn(id->valuestring+9,"0123456789abcdef")==8 &&
       cJSON_IsString(kind) && strlen(kind->valuestring)<sizeof(((ack_event_t *)0)->kind) &&
       cJSON_IsNumber(version) && version->valuedouble==2 && cJSON_IsBool(retry) &&
       cJSON_IsString(timestamp) && strlen(timestamp->valuestring)>=20 && strlen(timestamp->valuestring)<=32 &&
       ((success && cJSON_IsNull(code) && cJSON_IsFalse(retry)) ||
        (failed && cJSON_IsString(code) && safe_error_code(code->valuestring)))) {
        ack_event_t ack={.from_server=true,.success=success,.retryable=cJSON_IsTrue(retry)};
        strcpy(ack.id,id->valuestring);
        strcpy(ack.kind,kind->valuestring);
        if(cJSON_IsString(code) && safe_error_code(code->valuestring)) strcpy(ack.error,code->valuestring);
        if(xQueueSend(ack_queue,&ack,0)!=pdTRUE) atomic_fetch_add(&dropped,1);
    } else {
        ESP_LOGW("RX","Invalid ACK envelope topic=%s",ack_topic);
    }
    cJSON_Delete(j);
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
    if(id==MQTT_EVENT_PUBLISHED) {
        ack_event_t ack={.packet=e->msg_id};
        if(xQueueSend(ack_queue,&ack,0)!=pdTRUE) atomic_fetch_add(&dropped,1);
    } else if(id==MQTT_EVENT_ERROR) {
        mqtt_error(e->error_handle);
    } else if(id==MQTT_EVENT_CONNECTED) {
        ack_subscription_id=esp_mqtt_client_subscribe(client,ack_topic,1);
        atomic_store(&ack_subscription_ready,false);
        if(ack_subscription_id<0)
            ESP_LOGW("MQTT","ACK subscribe failed topic=%s",ack_topic);
        if(sc_serial_debug()) ESP_LOGI("MQTT","CONNECTED ACK topic=%s",ack_topic);
        else ESP_LOGI("MQTT","CONNECTED");
        xEventGroupSetBits(network,MQTT_UP);
        if(sc_command_auth_ready() && esp_mqtt_client_subscribe(client,SC_BASE "orders",1)<0)
            sc_log("WARN","MQTT_ERROR","Order subscription enqueue failed");
    } else if(id==MQTT_EVENT_SUBSCRIBED && e->msg_id==ack_subscription_id) {
        atomic_store(&ack_subscription_ready,true);
        ESP_LOGI("MQTT","ACK subscription ready topic=%s",ack_topic);
        sc_log("INFO","MQTT_CONNECTED","MQTT session connected");
    } else if(id==MQTT_EVENT_DISCONNECTED) {
        atomic_store(&ack_subscription_ready,false);
        ack_subscription_id=-1;
        xEventGroupClearBits(network,MQTT_UP);
        atomic_fetch_add(&connection_epoch,1);
        xQueueReset(rx_queue);
        incoming_valid=false;
        ESP_LOGW("MQTT","DISCONNECTED, waiting for reconnect");
        sc_log("WARN","MQTT_DISCONNECTED","MQTT session disconnected");
    } else if(id==MQTT_EVENT_DATA && !e->current_data_offset && topic_is(e,ack_topic)) {
        receive_ack(e);
    } else if(id==MQTT_EVENT_DATA && sc_network_ready() && sc_command_auth_ready()) {
        if(e->current_data_offset==0) {
            incoming_used=0; incoming_total=e->total_data_len;
            incoming_epoch=sc_network_epoch();
            bool supported=topic_is(e,SC_BASE "orders");
            incoming_valid=supported && !e->retain && incoming_total>0 &&
                incoming_total<sizeof(incoming);
            if(incoming_valid && sc_serial_debug())
                ESP_LOGI("RX","topic=%s bytes=%d (control payload omitted)",
                         SC_BASE "orders",incoming_total);
        }
        if(!incoming_valid || incoming_epoch!=sc_network_epoch() ||
           e->current_data_offset!=incoming_used || e->data_len<0 ||
           e->data_len>incoming_total-incoming_used) { incoming_valid=false; return; }
        memcpy(incoming+incoming_used,e->data,e->data_len); incoming_used+=e->data_len;
        if(incoming_used==incoming_total) {
            incoming[incoming_used]=0;
            sc_command_t cmd={.network_epoch=incoming_epoch};
            if(memchr(incoming,0,incoming_used)) { incoming_valid=false; return; }
            memcpy(cmd.data,incoming,incoming_used+1);
            if(xQueueSend(rx_queue,&cmd,0)!=pdTRUE) atomic_fetch_add(&dropped,1);
            incoming_valid=false;
        }
    }
}
static void wifi_event(void *arg,esp_event_base_t base,int32_t id,void *data)
{
    if(base==WIFI_EVENT && id==WIFI_EVENT_STA_START) provisioning_start();
    else if(base==WIFI_EVENT && id==WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(network,WIFI_UP|MQTT_UP);
        atomic_fetch_add(&connection_epoch,1);
        if(wifi_cb) wifi_cb(false);
        xEventGroupSetBits(network,WIFI_LOG_DOWN);
        provisioning_wifi_disconnected();
    } else if(base==IP_EVENT && id==IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(network,WIFI_UP|WIFI_LOG_UP);
        provisioning_wifi_connected();
        if(wifi_cb) wifi_cb(true);
    }
}
static void report_wifi_events(void)
{
    EventBits_t events=xEventGroupWaitBits(network,WIFI_LOG_UP|WIFI_LOG_DOWN,pdTRUE,pdFALSE,0);
    if(events&WIFI_LOG_DOWN) {
        ESP_LOGW("WIFI","DISCONNECTED, reconnecting");
        sc_log("WARN","WIFI_DISCONNECTED","Wi-Fi disconnected; reconnecting");
    }
    if(events&WIFI_LOG_UP) {
        esp_netif_t *sta=esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        esp_netif_ip_info_t ip;
        wifi_ap_record_t ap;
        if(sta && esp_netif_get_ip_info(sta,&ip)==ESP_OK &&
           esp_wifi_sta_get_ap_info(&ap)==ESP_OK) {
            ESP_LOGI("WIFI","IP=" IPSTR " RSSI=%d dBm",IP2STR(&ip.ip),ap.rssi);
        }
        sc_log("INFO","WIFI_CONNECTED","Wi-Fi obtained IP address");
    }
}
static void local_stamp(char *out,size_t capacity)
{
    time_t clock_now=time(NULL);
    if(clock_now>1735689600) {
        struct tm local;
        clock_now+=7*60*60;
        if(gmtime_r(&clock_now,&local) && strftime(out,capacity,"%H:%M:%S",&local)) return;
    }
    snprintf(out,capacity,"+%"PRId64"s",esp_timer_get_time()/1000000);
}
static unsigned short_id(const char *id)
{
    return (unsigned)((uint32_t)strtoul(id+9,NULL,16)-(nonce[1]&0x7fffffffU))%1000;
}
static double own_number(const char *json,const char *key)
{
    const char *value=strstr(json,key);
    return value?strtod(value+strlen(key),NULL):0;
}
static void serial_tx(const pending_t *p)
{
    if(sc_serial_debug()) {
        ESP_LOGI("TX","id=%s topic=%s retry=%u JSON %s",p->message.id,
                 p->message.topic,p->attempts,p->message.json);
        return;
    }
    char stamp[24];
    local_stamp(stamp,sizeof(stamp));
    unsigned seq=short_id(p->message.id);
    if(p->attempts) {
        ESP_LOGI("TX","[%s] #%03u | RETRY %u/3",stamp,seq,p->attempts);
    } else if(!strcmp(p->message.kind,"heartbeat")) {
        ESP_LOGI("TX","[%s] HB #%03u | RAM=%u KiB | RSSI=%d",stamp,seq,
                 (unsigned)own_number(p->message.json,"\"free_heap\":")/1024,
                 (int)own_number(p->message.json,"\"rssi\":"));
    } else if(!strcmp(p->message.kind,"telemetry")) {
        const char *json=p->message.json;
        ESP_LOGI("TX","[%s] DATA #%03u | %.1fV | %.2fA | %.1fW | %.2fWh",stamp,seq,
                 own_number(json,"\"voltage_v\":"),own_number(json,"\"current_a\":"),
                 own_number(json,"\"power_w\":"),own_number(json,"\"energy_total_wh\":"));
    } else {
        ESP_LOGI("TX","[%s] #%03u | %s",stamp,seq,p->message.kind);
    }
}
static void serial_ack(const pending_t *p,const ack_event_t *a,int64_t now)
{
    int64_t elapsed=(now-p->sent_at)/1000;
    if(sc_serial_debug() || !a->success) {
        ESP_LOG_LEVEL(a->success?ESP_LOG_INFO:ESP_LOG_WARN,"ACK",
            "SERVER_%s id=%s type=%s rtt_ms=%"PRId64" retries=%u error=%s retryable=%d",
            a->success?"SUCCESS":"FAILED",a->id,a->kind,elapsed,p->attempts-1,
            a->error[0]?a->error:"none",a->retryable);
    } else {
        char stamp[24];
        local_stamp(stamp,sizeof(stamp));
        ESP_LOGI("ACK","[%s] #%03u | SUCCESS | %"PRId64"ms",stamp,short_id(a->id),elapsed);
    }
}
static bool prepare_next_boot_event(outbound_t *message)
{
    if(!atomic_load(&ack_subscription_ready) || boot_event_index>=3) return false;
    char reason[64];
    switch(boot_event_index) {
    case 0:
        if(!prepare_log(message,"INFO","BOOT","Application started")) return false;
        break;
    case 1:
        if(!prepare_log(message,"INFO","RELAY_OFF","Relay OFF at startup")) return false;
        break;
    default: {
        esp_reset_reason_t reset=esp_reset_reason();
        snprintf(reason,sizeof(reason),"Reset reason=%d",reset);
        bool watchdog=reset==ESP_RST_TASK_WDT || reset==ESP_RST_INT_WDT || reset==ESP_RST_WDT;
        if(!prepare_log(message,watchdog?"WARN":"INFO",
                        watchdog?"WATCHDOG_RESET":"REBOOT_REASON",reason)) return false;
        break;
    }
    }
    boot_event_index++;
    return true;
}
static void transport_settled(pending_t *p)
{
    if(!p->transport_done && p->message.wait_for_transport) atomic_fetch_sub(&transport_wait,1);
    p->transport_done=true;
}
static void finish_pending(pending_t *p)
{
    transport_settled(p);
    p->active=false;
}
static void handle_ack_event(const ack_event_t *a,int64_t now)
{
    for(size_t i=0;i<SC_ACK_SLOTS;i++) {
        pending_t *p=&pending[i];
        if(!p->active || !p->attempts) continue;
        if(a->from_server) {
            if(strcmp(a->id,p->message.id) || strcmp(a->kind,p->message.kind)) continue;
            if(p->server_terminal || (!a->success && a->retryable && p->retry_requested)) return;
            server_v2_seen=true;
            if(a->success) stats_ok++;
            else if(!a->retryable) stats_fail++;
            serial_ack(p,a,now);
            if(a->success || !a->retryable) {
                p->server_terminal=true;
                if(p->broker_ack) finish_pending(p);
            } else {
                p->retry_requested=true;
                p->due=now+1000000;
            }
            return;
        }
        if(p->packet!=a->packet || p->broker_ack) continue;
        p->broker_ack=true;
        transport_settled(p);
        if(sc_serial_debug()) ESP_LOGI("ACK","BROKER_ACK id=%s packet=%d rtt_ms=%"PRId64" retries=%u",
                 p->message.id,a->packet,(now-p->sent_at)/1000,p->attempts-1);
        if(p->server_terminal) finish_pending(p);
        return;
    }
    if(a->from_server) ESP_LOGD("ACK","Ignored late, duplicate or unmatched server ACK");
}
static void service_pending(int64_t now)
{
    ack_event_t ack;
    for(unsigned i=0;i<12 && xQueueReceive(ack_queue,&ack,0)==pdTRUE;i++) handle_ack_event(&ack,now);
    for(size_t i=0;i<SC_ACK_SLOTS;i++) {
        pending_t *p=&pending[i];
        if(!p->active) {
            if(!prepare_next_boot_event(&p->message) &&
               xQueueReceive(tx_queue,&p->message,0)!=pdTRUE) continue;
            p->active=true;
            p->broker_ack=p->server_terminal=p->transport_done=p->retry_requested=false;
            p->attempts=0;
            p->due=now;
        }
        if(now<p->due) continue;
        if(p->server_terminal) {
            ESP_LOGW("ACK","BROKER_ACK_NOT_OBSERVED id=%s (server replied)",p->message.id);
            finish_pending(p);
            continue;
        }
        /* Application ACK on the public broker is diagnostic only. Never use it
         * to authorize relay, payment, or another state transition. */
        bool can_retry=!p->message.state_changing || server_v2_seen;
        if(p->attempts>=SC_ACK_MAX_ATTEMPTS || (p->attempts && !can_retry) ||
           now-p->message.queued_at>=SC_ACK_LIFETIME_US) {
            ESP_LOGW("ACK","%s id=%s attempts=%u broker_ack=%d online=%d reason=%s",
                !p->attempts?"FAILED_NOT_SENT":"FAILED_NO_ACK",
                p->message.id,
                p->attempts,p->broker_ack,sc_network_ready(),
                p->attempts && !can_retry?"peer_not_confirmed_v2":
                p->attempts>=SC_ACK_MAX_ATTEMPTS?"retry_limit":"queue_lifetime");
            atomic_fetch_add(&dropped,1);
            stats_fail++;
            finish_pending(p);
            continue;
        }
        if(!sc_network_ready() || esp_mqtt_client_get_outbox_size(client)>=3072) {
            if(sc_network_ready())
                ESP_LOGW("TX","WAIT_OUTBOX_FULL id=%s retries=%u",p->message.id,p->attempts);
            else ESP_LOGW("TX","WAIT_DISCONNECTED id=%s attempts=%u",p->message.id,p->attempts);
            p->due=now+2000000;
            continue;
        }
        int packet=esp_mqtt_client_enqueue(client,p->message.topic,p->message.json,0,1,0,true);
        if(packet<=0) {
            ESP_LOGW("TX","FAILED_MQTT_ENQUEUE id=%s code=%d",p->message.id,packet);
            p->due=now+2000000;
            continue;
        }
        serial_tx(p);
        stats_tx++;
        if(p->attempts) stats_retry++;
        p->packet=packet;
        p->broker_ack=p->retry_requested=false;
        p->attempts++;
        p->sent_at=now;
        p->due=now+SC_ACK_TIMEOUT_US;
        if(sc_serial_debug()) ESP_LOGI("TX","PUBLISH_SUBMITTED id=%s packet=%d retries=%u (MQTT outbox)",
                 p->message.id,packet,p->attempts-1);
    }
}
static void heartbeat(void)
{
    if(!sc_network_ready()) return;
    wifi_ap_record_t ap;
    int rssi=esp_wifi_sta_get_ap_info(&ap)==ESP_OK?ap.rssi:0;
    unsigned h=atomic_load(&health);
    const char *mode=headless?"HEADLESS":"NORMAL";
    bool disabled=headless;
    char json[512];
    snprintf(json,sizeof(json),"{\"firmware_version\":\"%.31s\","
        "\"free_heap\":%u,\"min_free_heap\":%u,\"rssi\":%d,\"mqtt_connected\":true,"
        "\"relay_on\":%s,\"sensor_status\":\"%s\",\"display_status\":\"%s\","
        "\"dropped_messages\":%"PRIu32",\"reset_reason\":%d,\"mode\":\"%s\"}",
        esp_app_get_description()->version,
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT),
        (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT),rssi,
        gpio_get_level(GPIO_NUM_25)?"true":"false",
        disabled?"DISABLED":(h&1)?"OK":"UNAVAILABLE",
        disabled?"DISABLED":(h&2)?"OK":"UNAVAILABLE",
        sc_dropped_messages(),esp_reset_reason(),mode);
    sc_publish(SC_BASE "heartbeat",json,false);
}
static void serial_commands(void)
{
    if(!serial_rx_ready) return;
    static char command[24];
    static unsigned length;
    static bool overflow;
    uint8_t input[32];
    int count=uart_read_bytes(UART_NUM_0,input,sizeof(input),0);
    for(int i=0;i<count;i++) {
        if(input[i]!='\r' && input[i]!='\n') {
            if(length<sizeof(command)-1 && input[i]>=32 && input[i]<=126 && !overflow)
                command[length++]=(char)input[i];
            else overflow=true;
            continue;
        }
        if(overflow) {
            length=0;
            overflow=false;
            ESP_LOGW("USB","Invalid command. Use HELP.");
            continue;
        }
        if(!length) continue;
        command[length]=0;
        length=0;
        serial_mode_t mode=SERIAL_NORMAL;
        if(!strcmp(command,"LOG DEBUG")) mode=SERIAL_DEBUG;
        else if(!strcmp(command,"LOG ERROR")) mode=SERIAL_ERROR;
        else if(strcmp(command,"LOG NORMAL")) {
            if(!strcmp(command,"HELP")) ESP_LOGW("USB","LOG NORMAL | LOG DEBUG | LOG ERROR | STATUS | HELP");
            else if(!strcmp(command,"STATUS")) {
                ESP_LOGW("USB","device=%s alias=%s mode=%u mqtt=%d queue=%u dropped=%"PRIu32,
                         sc_logical_device(),sc_routing_device(),atomic_load(&serial_mode),sc_network_ready(),
                         (unsigned)uxQueueMessagesWaiting(tx_queue),sc_dropped_messages());
            } else ESP_LOGW("USB","Unknown command. Use HELP.");
            continue;
        }
        atomic_store(&serial_mode,mode);
        esp_log_level_set("*",mode==SERIAL_ERROR?ESP_LOG_WARN:ESP_LOG_INFO);
        if(mode==SERIAL_NORMAL) esp_log_level_set("wifi",ESP_LOG_WARN);
        ESP_LOGW("USB","%s",command);
    }
}
static void network_worker(void *arg)
{
    bool mqtt_started=false;
    int64_t last_diag=0,last_summary=0,next_start=0;
    while(1) {
        /* TLS certificate validity checks need trusted wall time from SNTP. */
        int64_t now=esp_timer_get_time();
        if(!mqtt_started && now>=next_start && (xEventGroupGetBits(network)&WIFI_UP) && time(NULL)>1735689600) {
            ESP_LOGI("WIFI","SNTP time available; starting MQTT");
            mqtt_started=esp_mqtt_client_start(client)==ESP_OK;
            next_start=now+5000000;
            if(!mqtt_started) sc_log("WARN","MQTT_ERROR","Failed to start MQTT task");
        }
        serial_commands();
        report_wifi_events();
        service_pending(now);
        if(now-last_diag>=10000000LL) {
            if(sc_serial_debug()) sc_memory_log();
            heartbeat();
            last_diag=now;
        }
        if(now-last_summary>=60000000LL) {
            unsigned caps=MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT;
            ESP_LOGI("MEM","TX=%u SUCCESS=%u FAILED=%u RETRY=%u DROPPED=%"PRIu32" FREE_HEAP=%u MIN_HEAP=%u",
                     stats_tx,stats_ok,stats_fail,stats_retry,sc_dropped_messages(),
                     (unsigned)heap_caps_get_free_size(caps),(unsigned)heap_caps_get_minimum_free_size(caps));
            last_summary=now;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
void sc_wait_outbox(unsigned timeout_ms)
{
    int64_t until=esp_timer_get_time()+(int64_t)timeout_ms*1000;
    while(esp_timer_get_time()<until) {
        if(tx_queue && uxQueueMessagesWaiting(tx_queue)==0 && !atomic_load(&transport_wait) &&
           client && esp_mqtt_client_get_outbox_size(client)==0) break;
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
void sc_network_start_headless(void)
{
    headless=true;
    sc_network_start(NULL,NULL);
}
void sc_network_start(void (*cb)(bool),void (*show_setup)(const char *,const char *))
{
    wifi_cb=cb;
    snprintf(mqtt_base,sizeof(mqtt_base),SC_PUBLIC_PREFIX "%s/",sc_routing_device());
    snprintf(ack_topic,sizeof(ack_topic),"%sack",mqtt_base);
    tx_queue=xQueueCreate(5,sizeof(outbound_t)); rx_queue=xQueueCreate(3,sizeof(sc_command_t));
    ack_queue=xQueueCreate(12,sizeof(ack_event_t));
    network=xEventGroupCreate();
    ESP_ERROR_CHECK(tx_queue && rx_queue && ack_queue && network?ESP_OK:ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_netif_init()); ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_create_default_wifi_sta()?ESP_OK:ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_netif_create_default_wifi_ap()?ESP_OK:ESP_ERR_NO_MEM);
    wifi_init_config_t init=WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,wifi_event,NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,wifi_event,NULL));
    ESP_ERROR_CHECK(provisioning_init(show_setup));
    wifi_config_t wifi={0};
    memcpy(wifi.sta.ssid,sc_config.ssid,strlen(sc_config.ssid));
    memcpy(wifi.sta.password,sc_config.password,strlen(sc_config.password));
    wifi.sta.threshold.authmode=WIFI_AUTH_WPA2_PSK;
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA,&wifi));
    esp_sntp_config_t sntp=ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    ESP_ERROR_CHECK(esp_netif_sntp_init(&sntp));
    snprintf(mqtt_client_id,sizeof(mqtt_client_id),"sc-%08lx",(unsigned long)esp_random());
    esp_mqtt_client_config_t mqtt={
        .broker.address.uri=SC_MQTT_BROKER,
        .broker.verification.crt_bundle_attach=esp_crt_bundle_attach,
        .credentials.client_id=mqtt_client_id,
        .session.keepalive=15,
        .buffer.size=1024, .buffer.out_size=1024,
        .task.stack_size=6144, .outbox.limit=4096,
        .network.timeout_ms=5000, .network.reconnect_timeout_ms=5000,
    };
    esp_fill_random(nonce,sizeof(nonce));
    atomic_store(&tx_sequence,nonce[1]&0x7fffffffU);
    esp_log_level_set("wifi",ESP_LOG_WARN);
    serial_rx_ready=uart_driver_install(UART_NUM_0,256,0,0,NULL,0)==ESP_OK;
    if(!serial_rx_ready) ESP_LOGW("USB","Serial command input unavailable");
    ESP_LOGI("BOOT","%s | FW %.31s | %s | reset=%d",sc_logical_device(),
             esp_app_get_description()->version,headless?"HEADLESS":"NORMAL",esp_reset_reason());
    ESP_LOGI("MQTT","Public route alias=%s base=%s",sc_routing_device(),mqtt_base);
    client=esp_mqtt_client_init(&mqtt);
    ESP_ERROR_CHECK(client?ESP_OK:ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(client,ESP_EVENT_ANY_ID,mqtt_event,NULL));
    ESP_ERROR_CHECK(xTaskCreate(network_worker,"sc_network",5120,NULL,3,NULL)==pdPASS?ESP_OK:ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(esp_wifi_start());
}
