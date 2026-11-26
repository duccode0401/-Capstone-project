"""Run the production MQTT pending state machine with fake MQTT/FreeRTOS I/O."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MqttAckTests(unittest.TestCase):
    def test_pending_state_machine(self):
        local = ROOT / "build-host/tcc/tcc.exe"
        cc = os.getenv("CC") or (str(local) if local.is_file() else shutil.which("cc"))
        if not cc:
            self.skipTest("Native C compiler required")
        network = (ROOT / "components/sc_core/sc_network.c").read_text(encoding="utf-8")
        types = network[network.index("#define SC_ACK_SLOTS"):network.index("static QueueHandle_t")]
        pending = network[network.index("static void transport_settled"):network.index("static void heartbeat")]
        wrapper = r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#define pdTRUE 1
#define atomic_fetch_sub(p,n) (*(p)-=(n))
#define atomic_fetch_add(p,n) (*(p)+=(n))
#define ESP_LOGI(tag,fmt,...) ((void)0)
#define ESP_LOGW(tag,fmt,...) ((void)0)
#define ESP_LOGD(tag,fmt,...) ((void)0)
#define ESP_LOG_LEVEL(level,tag,fmt,...) ((void)0)
'''
        wrapper += types + r'''
static pending_t pending[SC_ACK_SLOTS];
static unsigned dropped, transport_wait,stats_tx,stats_ok,stats_fail,stats_retry;
static bool sc_serial_debug(void) { return false; }
static void serial_tx(const pending_t *p) {}
static void serial_ack(const pending_t *p,const ack_event_t *a,int64_t now) {}
static char ack_topic[80];
static int ack_queue, tx_queue, client;
static unsigned publishes;
static bool connected,server_v2_seen;
static int enqueue_status=1, outbox_size;
static bool sc_network_ready(void) { return connected; }
static bool xQueueReceive(int q,void *out,int ticks) { return false; }
static int esp_mqtt_client_get_outbox_size(int c) { return outbox_size; }
static int esp_mqtt_client_enqueue(int c,const char *topic,const char *json,int len,int qos,int retain,bool store) {
    assert(qos==1 && !retain);
    assert(!strcmp(json,"same body"));
    if(enqueue_status<0) return enqueue_status;
    return ++publishes;
}
'''
        wrapper += pending + r'''
static void reset(void) {
    memset(pending,0,sizeof(pending));
    dropped=0; publishes=0; transport_wait=1; connected=true; enqueue_status=1; outbox_size=0;
    server_v2_seen=false;
    stats_tx=stats_ok=stats_fail=stats_retry=0;
    pending[0].active=true;
    pending[0].message.wait_for_transport=true;
    pending[0].message.kind="heartbeat";
    strcpy(pending[0].message.id,"321");
    strcpy(pending[0].message.json,"same body");
}
int main(void) {
    pending_t *p=&pending[0];
    ack_event_t broker={.packet=1};
    ack_event_t server={.from_server=true,.success=true};
    strcpy(server.id,"321");
    strcpy(server.kind,"heartbeat");

    reset();
    service_pending(0);
    assert(publishes==1 && p->active && p->due==5000000);
    handle_ack_event(&broker,1000);
    assert(p->active && p->broker_ack && transport_wait==0);
    strcpy(server.id,"wrong");
    handle_ack_event(&server,2000);
    assert(p->active && !p->server_terminal);
    strcpy(server.id,"321");
    strcpy(server.kind,"telemetry");
    handle_ack_event(&server,2000);
    assert(p->active && !p->server_terminal);
    strcpy(server.kind,"heartbeat");
    handle_ack_event(&server,2000);
    assert(!p->active);
    handle_ack_event(&server,3000);
    assert(transport_wait==0);

    reset();
    service_pending(0);
    handle_ack_event(&server,2000);
    assert(p->active && p->server_terminal && !p->broker_ack);
    handle_ack_event(&broker,3000);
    assert(!p->active);

    reset();
    service_pending(0);
    handle_ack_event(&broker,1000);
    for(int i=1;i<=4;i++) service_pending((int64_t)i*5000000);
    assert(!p->active && publishes==4 && dropped==1 && transport_wait==0);

    reset();
    service_pending(0);
    server.success=false;
    handle_ack_event(&server,2000);
    handle_ack_event(&broker,3000);
    assert(!p->active && publishes==1);

    reset();
    service_pending(0);
    server.retryable=true;
    handle_ack_event(&server,2000);
    handle_ack_event(&server,4000);
    assert(p->due==1002000);
    service_pending(1002000);
    assert(publishes==2);

    reset();
    p->message.state_changing=true;
    service_pending(0);
    service_pending(5000000);
    assert(!p->active && publishes==1);
    reset();
    connected=false;
    service_pending(0);
    assert(publishes==0 && p->due==2000000);
    connected=true;
    outbox_size=4096;
    service_pending(2000000);
    assert(publishes==0 && p->due==4000000);
    outbox_size=0;
    enqueue_status=-1;
    service_pending(4000000);
    assert(p->attempts==0 && p->due==6000000);
    enqueue_status=1;
    service_pending(6000000);
    assert(publishes==1 && p->attempts==1);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="sc-mqtt-ack-") as tmp:
            test = Path(tmp) / "ack.c"
            binary = Path(tmp) / ("ack.exe" if os.name == "nt" else "ack")
            test.write_text(wrapper, encoding="ascii")
            subprocess.run([cc, str(test), "-o", str(binary)], check=True, capture_output=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_wire_envelope_and_ack_validation(self):
        local = ROOT / "build-host/tcc/tcc.exe"
        cc = os.getenv("CC") or (str(local) if local.is_file() else shutil.which("cc"))
        idf = os.getenv("IDF_PATH")
        if not cc or not idf:
            self.skipTest("Native C compiler and ESP-IDF cJSON required")
        cjson = Path(idf) / "components/json/cJSON"
        network = (ROOT / "components/sc_core/sc_network.c").read_text(encoding="utf-8")
        core = (ROOT / "components/sc_core/sc_core.c").read_text(encoding="utf-8")
        types = network[network.index("#define SC_ACK_SLOTS"):network.index("static QueueHandle_t")]
        publish = network[network.index("bool sc_publish("):network.index("void sc_log(")]
        receive = network[network.index("static bool safe_error_code"):network.index("static void mqtt_error")]
        parser = core[core.index("cJSON *sc_parse_flat_json("):core.index("void sc_relay_off(")]
        wrapper = r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <inttypes.h>
#include "cJSON.h"
#define SC_BASE "iot/charge_station/v1/"
#define pdTRUE 1
#define ESP_LOGI(tag,fmt,...) ((void)0)
#define ESP_LOGW(tag,fmt,...) ((void)0)
#define atomic_fetch_add(p,n) (*(p)+=(n))
#define atomic_fetch_sub(p,n) (*(p)-=(n))
#define atomic_load(p) (*(p))
#define atomic_compare_exchange_weak(p,old,next) (*(p)=(next),true)
typedef int BaseType_t;
typedef struct {
    int retain,current_data_offset,data_len,total_data_len;
    const char *data;
} *esp_mqtt_event_handle_t;
'''
        wrapper += types + r'''
static struct { char device[32], mqtt_alias[25]; } sc_config={"ESP32_001","SC_TEST_0123456789abcdef"};
static uint32_t nonce[]={0xa83f921b,0x01234567};
static unsigned tx_sequence, transport_wait, dropped;
static int tx_queue=1,ack_queue=2;
static bool allow_queue=true;
static outbound_t sent;
static ack_event_t ack;
static char ack_topic[80];
static char mqtt_base[64]="iot/charge_station/test/SC_TEST_0123456789abcdef/";
static bool got_ack;
static int64_t esp_timer_get_time(void) { return 10000000; }
static bool sc_network_ready(void) { return true; }
static bool sc_serial_debug(void) { return false; }
static const char *sc_logical_device(void) { return sc_config.device; }
static const char *sc_routing_device(void) { return sc_config.mqtt_alias; }
static BaseType_t xQueueSend(int q,const void *item,int ticks) {
    if(!allow_queue) return false;
    if(q==tx_queue) memcpy(&sent,item,sizeof(sent));
    if(q==ack_queue) { memcpy(&ack,item,sizeof(ack)); got_ack=true; }
    return true;
}
'''
        wrapper += parser + publish + receive + r'''
static void server(const char *device, bool retained, bool valid) {
    char payload[384];
    snprintf(payload,sizeof(payload),
        "{\"protocol_version\":2,\"device\":\"%s\",\"message_id\":\"%s\","
        "\"message_type\":\"telemetry\",\"status\":\"SUCCESS\",\"retryable\":false,\"error_code\":null,"
        "\"server_timestamp\":\"2026-11-26T02:00:00Z\"}",device,sent.id);
    struct {int retain,current_data_offset,data_len,total_data_len; const char *data;} event={
        retained,0,strlen(payload),strlen(payload),payload};
    got_ack=false;
    receive_ack((esp_mqtt_event_handle_t)&event);
    assert(got_ack==valid);
}
int main(void) {
    const char *data="{\"voltage_v\":1.5}";
    assert(sc_publish(SC_BASE "telemetry",data,false));
    assert(!strcmp(sent.topic,"iot/charge_station/test/SC_TEST_0123456789abcdef/telemetry"));
    cJSON *json=sc_parse_flat_json(sent.json,sizeof(sent.json)-1);
    assert(json);
    assert(cJSON_GetObjectItemCaseSensitive(json,"protocol_version")->valueint==2);
    assert(strlen(sent.id)==17 && sent.id[8]=='-');
    assert(!strcmp(cJSON_GetObjectItemCaseSensitive(json,"device")->valuestring,"SC_TEST_0123456789abcdef"));
    assert(!strcmp(cJSON_GetObjectItemCaseSensitive(json,"logical_device")->valuestring,"ESP32_001"));
    assert(cJSON_GetObjectItemCaseSensitive(json,"uptime_ms")->valueint==10000);
    assert(cJSON_GetObjectItemCaseSensitive(json,"voltage_v")->valuedouble==1.5);
    cJSON_Delete(json);
    server(sc_config.mqtt_alias,false,true);
    assert(ack.success && !ack.retryable);
    server("wrong-device",false,false);
    server(sc_config.mqtt_alias,true,false);
    char first[SC_ACK_ID_LEN]; strcpy(first,sent.id);
    nonce[0]++; tx_sequence=0;
    assert(sc_publish(SC_BASE "telemetry",data,false));
    assert(strcmp(first,sent.id));
    assert(!sc_publish(SC_BASE "status",data,true));
    allow_queue=false;
    assert(!sc_publish(SC_BASE "heartbeat",data,false));
    assert(dropped==1);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="sc-mqtt-wire-") as tmp:
            test = Path(tmp) / "ack.c"
            binary = Path(tmp) / ("ack.exe" if os.name == "nt" else "ack")
            test.write_text(wrapper, encoding="ascii")
            cmd = [cc, "-I", str(cjson), str(test), str(cjson / "cJSON.c"), "-o", str(binary)]
            if os.name != "nt":
                cmd.append("-lm")
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
