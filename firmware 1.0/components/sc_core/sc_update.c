#include "sc_core.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#include <inttypes.h>
#include <time.h>
#include <strings.h>
#include "cJSON.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_ota_ops.h"
#include "esp_app_desc.h"
#include "esp_app_format.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "mbedtls/sha256.h"

typedef struct { char url[384],sha[65]; int size; } manifest_t;
typedef struct {
    char content_type[80],encoding[32];
    bool invalid;
    unsigned type_count,length_count,encoding_count;
} http_headers_t;

const char *sc_update_error_name(esp_err_t error)
{
    if(error>SC_OTA_HTTP_ERROR_BASE && error<=SC_OTA_HTTP_ERROR_BASE+SC_HTTP_JSON_PREFIX)
        return sc_http_result_name(error-SC_OTA_HTTP_ERROR_BASE);
    if(error==SC_OTA_ERR_TLS_CONNECT) return "HTTPS_TLS_OR_CONNECT_FAILED";
    if(error==SC_OTA_ERR_DOWNLOAD_INTERRUPTED) return "HTTPS_DOWNLOAD_INTERRUPTED";
    return esp_err_to_name(error);
}
bool sc_update_retryable(esp_err_t error)
{
    /* A retry cannot repair authenticated corrupt images, signatures or an expired capability. */
    return error==SC_OTA_ERR_TLS_CONNECT || error==SC_OTA_ERR_DOWNLOAD_INTERRUPTED ||
           error==ESP_ERR_TIMEOUT || error==ESP_ERR_NO_MEM ||
           error==SC_OTA_HTTP_ERROR_BASE+SC_HTTP_STATUS ||
           error==SC_OTA_HTTP_ERROR_BASE+SC_HTTP_HTML;
}
static esp_err_t response_header(esp_http_client_event_t *event)
{
    if(event->event_id!=HTTP_EVENT_ON_HEADER || !event->user_data) return ESP_OK;
    http_headers_t *headers=event->user_data;
    char *destination=NULL; size_t capacity=0;
    if(!strcasecmp(event->header_key,"Content-Type")) {
        destination=headers->content_type; capacity=sizeof(headers->content_type);
        if(++headers->type_count>1) headers->invalid=true;
    } else if(!strcasecmp(event->header_key,"Content-Encoding")) {
        destination=headers->encoding; capacity=sizeof(headers->encoding);
        if(++headers->encoding_count>1) headers->invalid=true;
    } else if(!strcasecmp(event->header_key,"Content-Length")) {
        if(++headers->length_count>1) headers->invalid=true;
    }
    if(destination) {
        if(strlen(event->header_value)>=capacity) headers->invalid=true;
        else strcpy(destination,event->header_value);
    }
    return ESP_OK;
}
static void close_https(esp_http_client_handle_t h)
{
    if(h) { esp_http_client_close(h); esp_http_client_cleanup(h); }
}
static esp_err_t open_https(const char *url,int expected_max,bool image,
    esp_http_client_handle_t *out,http_headers_t *headers)
{
    *out=NULL; memset(headers,0,sizeof(*headers));
    if(!sc_trusted_url(url)) return ESP_ERR_INVALID_ARG;
    if(time(NULL)<1735689600) return ESP_ERR_INVALID_STATE;
    esp_http_client_config_t c={.url=url,.crt_bundle_attach=esp_crt_bundle_attach,
        .timeout_ms=10000,.buffer_size=1024,.buffer_size_tx=512,
        .disable_auto_redirect=true,.keep_alive_enable=false,
        .skip_cert_common_name_check=false,
        .event_handler=response_header,.user_data=headers,
        .user_agent="SmartCharge-OTA/1",
        .transport_type=HTTP_TRANSPORT_OVER_SSL};
    esp_http_client_handle_t h=esp_http_client_init(&c);
    if(!h) return ESP_ERR_NO_MEM;
    /* Client-side ngrok header, not an authentication bypass or a TLS exception. */
    esp_err_t e=esp_http_client_set_header(h,"ngrok-skip-browser-warning","1");
    if(e==ESP_OK) e=esp_http_client_set_header(h,"Accept-Encoding","identity");
    if(e==ESP_OK) e=esp_http_client_set_header(h,"Accept",image?"application/octet-stream":"application/json");
    if(e!=ESP_OK) { close_https(h); return e; }
    e=esp_http_client_open(h,0);
    if(e!=ESP_OK) {
        int tls_code=0,tls_flags=0;
        esp_http_client_get_and_clear_last_tls_error(h,&tls_code,&tls_flags);
        ESP_LOGW("OTA","HTTPS connect failed: %s tls_code=%d verify_flags=0x%x",esp_err_to_name(e),tls_code,tls_flags);
        close_https(h); return SC_OTA_ERR_TLS_CONNECT;
    }
    int64_t n=esp_http_client_fetch_headers(h);
    if(n<0) { close_https(h); return SC_OTA_ERR_DOWNLOAD_INTERRUPTED; }
    int status=esp_http_client_get_status_code(h);
    sc_http_result_t policy=sc_http_check_headers(status,n,esp_http_client_is_chunked_response(h),
        headers->content_type,headers->encoding,expected_max,image);
    if(headers->invalid || headers->length_count!=1) {
        if(policy==SC_HTTP_OK) policy=SC_HTTP_LENGTH;
    }
    if(policy!=SC_HTTP_OK) {
        /* No URL, Location, server body or capability is included in the diagnostic. */
        ESP_LOGW("OTA","%s HTTP status=%d rejected: %s",image?"Binary":"Manifest",status,sc_http_result_name(policy));
        close_https(h); return SC_OTA_HTTP_ERROR_BASE+policy;
    }
    *out=h; return ESP_OK;
}
static bool get_string(cJSON *j,const char *key,char *dst,size_t cap)
{
    cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);
    if(!cJSON_IsString(v) || strlen(v->valuestring)>=cap) return false;
    strcpy(dst,v->valuestring); return true;
}
static esp_err_t fetch_manifest(const sc_request_t *r,manifest_t *m)
{
    esp_http_client_handle_t h=NULL; http_headers_t headers;
    esp_err_t e=open_https(r->manifest_url,2048,false,&h,&headers);
    if(e!=ESP_OK) return e;
    int total=(int)esp_http_client_get_content_length(h),used=0;
    char *body=calloc(1,total+1);
    if(!body) { close_https(h); return ESP_ERR_NO_MEM; }
    while(used<total) {
        int n=esp_http_client_read(h,body+used,total-used);
        if(n<=0) break;
        used+=n;
    }
    bool complete=used==total && esp_http_client_is_complete_data_received(h);
    close_https(h);
    if(!complete) { free(body); return SC_OTA_ERR_DOWNLOAD_INTERRUPTED; }
    sc_http_result_t prefix=sc_http_check_prefix((const unsigned char *)body,total,false);
    if(prefix!=SC_HTTP_OK) { free(body); return SC_OTA_HTTP_ERROR_BASE+prefix; }
    if(memchr(body,0,total)) { free(body); return ESP_ERR_INVALID_RESPONSE; }
    cJSON *j=complete?sc_parse_flat_json(body,2048):NULL;
    free(body);
    if(!j) return ESP_ERR_INVALID_RESPONSE;
    char device[32],request[64],version[32],chip[16],signature[65],seq[16];
    cJSON *size=cJSON_GetObjectItemCaseSensitive(j,"size");
    bool ok=get_string(j,"device",device,sizeof(device)) && !strcmp(device,r->device) &&
        get_string(j,"request_id",request,sizeof(request)) && !strcmp(request,r->request_id) &&
        get_string(j,"version",version,sizeof(version)) && !strcmp(version,r->version) &&
        get_string(j,"chip",chip,sizeof(chip)) && !strcmp(chip,"esp32") &&
        get_string(j,"seq",seq,sizeof(seq)) &&
        get_string(j,"url",m->url,sizeof(m->url)) && sc_trusted_url(m->url) &&
        get_string(j,"sha256",m->sha,sizeof(m->sha)) && strlen(m->sha)==64 &&
        get_string(j,"signature",signature,sizeof(signature)) &&
        cJSON_IsNumber(size) && size->valuedouble==size->valueint && size->valueint>288 && size->valueint<0x2f0000;
    m->size=cJSON_IsNumber(size)?size->valueint:0;
    cJSON_Delete(j);
    char expected_seq[16]; snprintf(expected_seq,sizeof(expected_seq),"%"PRIu32,r->seq);
    if(!ok || strcmp(seq,expected_seq)) return ESP_ERR_INVALID_RESPONSE;
    for(int i=0;i<64;i++) if(!isxdigit((unsigned char)m->sha[i]) || isupper((unsigned char)m->sha[i])) return ESP_ERR_INVALID_RESPONSE;
    char canonical[896];
    snprintf(canonical,sizeof(canonical),"SC-MANIFEST-1\n%s\n%s\n%s\n%s\n%d\n%s\nesp32\n%"PRIu32,
             r->device,r->request_id,r->version,m->url,m->size,m->sha,r->seq);
    return sc_verify_hmac(canonical,signature)?ESP_OK:ESP_ERR_INVALID_CRC;
}
esp_err_t sc_install(const sc_request_t *r)
{
    const esp_partition_t *running=esp_ota_get_running_partition();
    const esp_partition_t *target=esp_partition_find_first(0,ESP_PARTITION_SUBTYPE_APP_OTA_0,NULL);
    if(!sc_ota_enabled() || !running || running->subtype!=ESP_PARTITION_SUBTYPE_APP_FACTORY ||
       !target || target->address!=0x110000 || target->size!=0x2f0000) return ESP_ERR_INVALID_STATE;
    /* Erase both otadata sectors, verify factory selected BEFORE any ota_0 write. */
    esp_err_t err=sc_go_recovery(); if(err!=ESP_OK) return err;
    err=sc_set_state("pending"); if(err!=ESP_OK) return err;
    manifest_t m={0};
    err=fetch_manifest(r,&m); if(err!=ESP_OK) return err;
    esp_http_client_handle_t h=NULL; http_headers_t headers;
    err=open_https(m.url,m.size,true,&h,&headers);
    if(err!=ESP_OK) return err;
    if(esp_http_client_get_content_length(h)!=m.size) { close_https(h); return ESP_ERR_INVALID_SIZE; }
    unsigned char *buf=malloc(1024);
    if(!buf) { close_https(h); return ESP_ERR_NO_MEM; }
    esp_ota_handle_t ota=0;
    mbedtls_sha256_context sha; mbedtls_sha256_init(&sha);
    err=mbedtls_sha256_starts(&sha,0)==0?ESP_OK:ESP_FAIL;
    int received=0,progress=-1,next_milestone=25;
    sc_status(r,"DOWNLOADING",0); sc_log("INFO","OTA_DOWNLOADING","Streaming authenticated firmware");
    ESP_LOGI("OTA","Firmware download 0%%");
    int64_t deadline=esp_timer_get_time()+600000000LL;
    while(err==ESP_OK && received<m.size) {
        if(esp_timer_get_time()>deadline) { err=ESP_ERR_TIMEOUT; break; }
        /* First buffer includes image header, first segment header and app descriptor. */
        int used=0,want=(m.size-received)<1024?(m.size-received):1024;
        while(used<want) {
            if(esp_timer_get_time()>deadline) { err=ESP_ERR_TIMEOUT; break; }
            int n=esp_http_client_read(h,(char *)buf+used,want-used);
            if(n<=0) { err=SC_OTA_ERR_DOWNLOAD_INTERRUPTED; break; }
            used+=n;
        }
        if(err!=ESP_OK) break;
        if(received==0) {
            sc_http_result_t prefix=sc_http_check_prefix(buf,used,true);
            if(prefix!=SC_HTTP_OK) { err=SC_OTA_HTTP_ERROR_BASE+prefix; break; }
            esp_image_header_t header; esp_app_desc_t desc;
            memcpy(&header,buf,sizeof(header));
            memcpy(&desc,buf+sizeof(header)+sizeof(esp_image_segment_header_t),sizeof(desc));
            if(header.magic!=ESP_IMAGE_HEADER_MAGIC || header.chip_id!=0 ||
               desc.magic_word!=ESP_APP_DESC_MAGIC_WORD || !memchr(desc.version,0,sizeof(desc.version)) ||
               strcmp(desc.version,r->version) || !memchr(desc.project_name,0,sizeof(desc.project_name)) ||
               strcmp(desc.project_name,"smartcharge")) { err=ESP_ERR_INVALID_VERSION; break; }
            err=esp_ota_begin(target,m.size,&ota); if(err!=ESP_OK) break;
        }
        if(mbedtls_sha256_update(&sha,buf,used)!=0) { err=ESP_FAIL; break; }
        err=esp_ota_write(ota,buf,used); if(err!=ESP_OK) break;
        received+=used;
        int p=(received*100/m.size)/10*10;
        if(p!=progress) {
            sc_status(r,"DOWNLOADING",p);
            progress=p;
        }
        if(received*100/m.size>=next_milestone) {
            char message[48];
            snprintf(message,sizeof(message),"Firmware download %d%%",next_milestone);
            ESP_LOGI("OTA","%s",message);
            sc_log("INFO","OTA_PROGRESS",message);
            next_milestone+=25;
        }
    }
    unsigned char digest[32]={0}; char hex[65];
    if(err==ESP_OK && (received!=m.size || !esp_http_client_is_complete_data_received(h))) err=SC_OTA_ERR_DOWNLOAD_INTERRUPTED;
    if(err==ESP_OK) {
        sc_status(r,"VERIFYING",100);
        if(mbedtls_sha256_finish(&sha,digest)!=0) err=ESP_FAIL;
        for(int i=0;i<32;i++) snprintf(hex+2*i,3,"%02x",digest[i]);
        if(err==ESP_OK && strcmp(hex,m.sha)) err=ESP_ERR_INVALID_CRC;
    }
    mbedtls_sha256_free(&sha); free(buf);
    close_https(h);
    if(err!=ESP_OK) { if(ota) esp_ota_abort(ota); return err; }
    /* esp_ota_end verifies chip revision, checksum and full ESP image structure. */
    err=esp_ota_end(ota); if(err!=ESP_OK) return err;
    sc_status(r,"INSTALLING",100);
    err=sc_set_state("candidate"); if(err!=ESP_OK) return err;
    /* Final commit point: only a complete, authenticated image is selected. */
    err=esp_ota_set_boot_partition(target); if(err!=ESP_OK) return err;
    sc_status(r,"REBOOTING",100); sc_wait_outbox(2000);
    esp_restart();
    return ESP_OK;
}
