#include "sc_provisioning.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_http_server.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "sc_core.h"
#include "sdkconfig.h"
#include "sc_storage.h"

static httpd_handle_t server;
static esp_timer_handle_t setup_timer;
static bool setup_mode,testing;
static unsigned failures,test_failures;
static char ap_ssid[33],candidate_ssid[33],candidate_password[65];
static wifi_config_t previous_station;
static void (*show_setup)(const char *,const char *);

static void stop_setup(void)
{
    if(!setup_mode) return;
    if(server) { httpd_stop(server); server=NULL; }
    esp_timer_stop(setup_timer);
    setup_mode=false;
    testing=false;
    memset(candidate_password,0,sizeof(candidate_password));
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_LOGI("WIFI","Setup Mode stopped");
}

static void setup_expired(void *context)
{
    (void)context;
    ESP_LOGW("WIFI","Setup Mode timed out; relay remains OFF until networking is safe");
    stop_setup();
    if(storage_has_wifi()) esp_wifi_connect();
}

static int hex_value(char value)
{
    if(value>='0' && value<='9') return value-'0';
    value=(char)tolower((unsigned char)value);
    return value>='a' && value<='f'?value-'a'+10:-1;
}

static bool form_value(const char *body,const char *key,char *out,size_t capacity)
{
    size_t key_length=strlen(key);
    for(const char *field=body;field && *field;field=strchr(field,'&')) {
        if(*field=='&') field++;
        if(strncmp(field,key,key_length) || field[key_length]!='=') continue;
        const char *input=field+key_length+1;
        size_t used=0;
        while(*input && *input!='&') {
            unsigned char value=(unsigned char)*input++;
            if(value=='+') value=' ';
            else if(value=='%' && input[0] && input[1]) {
                int high=hex_value(input[0]),low=hex_value(input[1]);
                if(high<0 || low<0) return false;
                value=(unsigned char)((high<<4)|low);
                input+=2;
            }
            if(!value || used+1>=capacity) return false;
            out[used++]=(char)value;
        }
        out[used]=0;
        return true;
    }
    return false;
}

static bool setup_key_valid(const char *value)
{
    const char *expected=storage_setup_password();
    size_t a=strlen(value),b=strlen(expected);
    unsigned difference=(unsigned)(a^b);
    for(size_t i=0;i<b;i++) difference|=(unsigned char)expected[i]^
        (unsigned char)(i<a?value[i]:0);
    return difference==0;
}

static esp_err_t root_handler(httpd_req_t *request)
{
    wifi_scan_config_t scan={.show_hidden=false};
    uint16_t count=0;
    wifi_ap_record_t access_points[12];
    esp_err_t scan_error=esp_wifi_scan_start(&scan,true);
    if(scan_error==ESP_OK) {
        count=12;
        esp_wifi_scan_get_ap_records(&count,access_points);
    }
    httpd_resp_set_type(request,"text/html; charset=utf-8");
    httpd_resp_sendstr_chunk(request,"<!doctype html><meta name=viewport content='width=device-width'>"
        "<title>SmartCharge Wi-Fi</title><h2>SmartCharge Wi-Fi Setup</h2>"
        "<p>Select a network. The saved password is never displayed.</p>"
        "<form method=post action=/configure><label>Wi-Fi:</label><select name=ssid>");
    for(uint16_t i=0;i<count;i++) {
        char escaped[140];
        size_t used=0;
        const unsigned char *ssid=access_points[i].ssid;
        for(size_t n=0;ssid[n] && used+6<sizeof(escaped);n++) {
            unsigned char c=ssid[n];
            if(c=='&') { memcpy(escaped+used,"&amp;",5); used+=5; }
            else if(c=='<') { memcpy(escaped+used,"&lt;",4); used+=4; }
            else if(c=='\"') { memcpy(escaped+used,"&quot;",6); used+=6; }
            else if(c>=32 && c<127) escaped[used++]=(char)c;
        }
        escaped[used]=0;
        char option[360];
        snprintf(option,sizeof(option),"<option value=\"%s\">%s (%d dBm)</option>",
                 escaped,escaped,access_points[i].rssi);
        httpd_resp_sendstr_chunk(request,option);
    }
    httpd_resp_sendstr_chunk(request,"</select><br><label>Wi-Fi password:</label>"
        "<input type=password name=password maxlength=64 autocomplete=new-password><br>"
        "<label>Setup password:</label><input type=password name=setup_key maxlength=16><br>"
        "<button type=submit>Test and save</button></form>");
    return httpd_resp_sendstr_chunk(request,NULL);
}

static esp_err_t configure_handler(httpd_req_t *request)
{
    if(request->content_len<=0 || request->content_len>256) {
        httpd_resp_send_err(request,HTTPD_400_BAD_REQUEST,"Invalid request"); return ESP_OK;
    }
    char body[257],key[17];
    int received=httpd_req_recv(request,body,request->content_len);
    if(received!=request->content_len) {
        httpd_resp_send_err(request,HTTPD_400_BAD_REQUEST,"Incomplete request"); return ESP_OK;
    }
    body[received]=0;
    if(!form_value(body,"ssid",candidate_ssid,sizeof(candidate_ssid)) || !candidate_ssid[0] ||
       !form_value(body,"password",candidate_password,sizeof(candidate_password)) ||
       !form_value(body,"setup_key",key,sizeof(key)) || !setup_key_valid(key)) {
        memset(candidate_password,0,sizeof(candidate_password));
        httpd_resp_send_err(request,HTTPD_403_FORBIDDEN,"Invalid setup data"); return ESP_OK;
    }
    esp_wifi_get_config(WIFI_IF_STA,&previous_station);
    wifi_config_t station={0};
    memcpy(station.sta.ssid,candidate_ssid,strlen(candidate_ssid));
    memcpy(station.sta.password,candidate_password,strlen(candidate_password));
    /* Accept the security mode selected by the AP. Setup itself remains WPA2;
     * TLS still authenticates MQTT independently. */
    station.sta.threshold.authmode=WIFI_AUTH_OPEN;
    station.sta.pmf_cfg.capable=true;
    testing=true;
    test_failures=0;
    sc_relay_off();
    esp_wifi_disconnect();
    esp_err_t error=esp_wifi_set_config(WIFI_IF_STA,&station);
    if(error==ESP_OK) error=esp_wifi_connect();
    memset(body,0,sizeof(body));
    memset(key,0,sizeof(key));
    if(error!=ESP_OK) {
        testing=false;
        memset(candidate_password,0,sizeof(candidate_password));
        httpd_resp_send_err(request,HTTPD_500_INTERNAL_SERVER_ERROR,"Connection test could not start");
    } else {
        httpd_resp_sendstr(request,"Testing the new network. It is saved only after ESP32 obtains an IP address.");
    }
    return ESP_OK;
}

static esp_err_t begin_setup(void)
{
    if(setup_mode) return ESP_OK;
    sc_relay_off();
    uint8_t mac[6];
    ESP_RETURN_ON_ERROR(esp_read_mac(mac,ESP_MAC_WIFI_SOFTAP),"WIFI","MAC unavailable");
    snprintf(ap_ssid,sizeof(ap_ssid),"SmartCharge-%02X%02X%02X",mac[3],mac[4],mac[5]);
    wifi_config_t ap={0};
    strcpy((char *)ap.ap.ssid,ap_ssid);
    strcpy((char *)ap.ap.password,storage_setup_password());
    ap.ap.ssid_len=strlen(ap_ssid);
    ap.ap.channel=1;
    ap.ap.max_connection=2;
    ap.ap.authmode=WIFI_AUTH_WPA2_PSK;
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_APSTA),"WIFI","APSTA mode failed");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_AP,&ap),"WIFI","AP config failed");
    httpd_config_t config=HTTPD_DEFAULT_CONFIG();
    config.stack_size=4096;
    config.max_open_sockets=2;
    ESP_RETURN_ON_ERROR(httpd_start(&server,&config),"WIFI","HTTP server failed");
    const httpd_uri_t root={.uri="/",.method=HTTP_GET,.handler=root_handler};
    const httpd_uri_t configure={.uri="/configure",.method=HTTP_POST,.handler=configure_handler};
    ESP_ERROR_CHECK(httpd_register_uri_handler(server,&root));
    ESP_ERROR_CHECK(httpd_register_uri_handler(server,&configure));
    setup_mode=true;
    ESP_ERROR_CHECK(esp_timer_start_once(setup_timer,(int64_t)CONFIG_SC_SETUP_TIMEOUT_MINUTES*60*1000000));
    ESP_LOGW("WIFI","Setup Mode active: SSID=%s URL=http://192.168.4.1 timeout=%d min",
             ap_ssid,CONFIG_SC_SETUP_TIMEOUT_MINUTES);
    if(show_setup) show_setup(ap_ssid,storage_setup_password());
    return ESP_OK;
}

esp_err_t provisioning_init(void (*show_credentials)(const char *,const char *))
{
    show_setup=show_credentials;
    const esp_timer_create_args_t timer={.callback=setup_expired,.name="wifi_setup"};
    return esp_timer_create(&timer,&setup_timer);
}

void provisioning_start(void)
{
    if(!storage_has_wifi()) ESP_ERROR_CHECK_WITHOUT_ABORT(begin_setup());
    else esp_wifi_connect();
}

void provisioning_wifi_disconnected(void)
{
    sc_relay_off();
    if(testing) {
        if(++test_failures<3) { esp_wifi_connect(); return; }
        testing=false;
        memset(candidate_password,0,sizeof(candidate_password));
        ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_set_config(WIFI_IF_STA,&previous_station));
        ESP_LOGW("WIFI","New network test failed; credentials were not saved");
        esp_wifi_connect();
        return;
    }
    if(setup_mode) return;
    if(++failures>=CONFIG_SC_WIFI_FAILURES_BEFORE_SETUP) ESP_ERROR_CHECK_WITHOUT_ABORT(begin_setup());
    else esp_wifi_connect();
}

void provisioning_wifi_connected(void)
{
    failures=0;
    if(testing) {
        esp_err_t error=storage_save_wifi(candidate_ssid,candidate_password);
        memset(candidate_password,0,sizeof(candidate_password));
        testing=false;
        if(error!=ESP_OK) {
            ESP_LOGE("STORAGE","Connected network could not be saved: %s",esp_err_to_name(error));
            return;
        }
        ESP_LOGI("WIFI","New network validated and saved");
        stop_setup();
    } else if(setup_mode) {
        ESP_LOGI("WIFI","Saved network recovered; leaving Setup Mode");
        stop_setup();
    }
}

bool provisioning_active(void) { return setup_mode; }
