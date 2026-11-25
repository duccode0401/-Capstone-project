#include "sc_core.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include "cJSON.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_ota_ops.h"
#include "esp_app_desc.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "mbedtls/md.h"
#include "sdkconfig.h"
#if !CONFIG_ESPTOOLPY_FLASHSIZE_4MB || !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE || !CONFIG_PARTITION_TABLE_CUSTOM
#error "SmartCharge/Recovery require the 4MB custom partition table and rollback-enabled standard bootloader"
#endif

sc_config_t sc_config;
_Static_assert(sizeof(sc_request_t)==584,"Persisted NVS request V1 ABI changed; immutable Recovery needs compatibility");
static nvs_handle_t store;
static bool storage_ready;
cJSON *sc_parse_flat_json(const char *text,size_t max_length)
{
    if(strnlen(text,max_length+1)>max_length) return NULL;
    bool quoted=false,escape=false;
    int depth=0;
    for(const unsigned char *p=(const unsigned char *)text;*p;p++) {
        if(quoted) {
            if(escape) escape=false;
            else if(*p=='\\') escape=true;
            else if(*p=='"') quoted=false;
        } else if(*p=='"') quoted=true;
        else if(*p=='{' || *p=='[') { if(++depth>1 || *p=='[') return NULL; }
        else if(*p=='}' || *p==']') { if(--depth<0) return NULL; }
    }
    if(quoted || depth) return NULL;
    cJSON *j=cJSON_ParseWithOpts(text,NULL,true);
    if(!cJSON_IsObject(j)) { cJSON_Delete(j); return NULL; }
    /* Reject duplicate keys so server and device cannot interpret different fields. */
    for(cJSON *a=j->child;a;a=a->next)
        for(cJSON *b=a->next;b;b=b->next)
            if(!strcmp(a->string,b->string)) { cJSON_Delete(j); return NULL; }
    return j;
}
static esp_err_t put_string(const char *key, const char *value)
{
    esp_err_t e = nvs_set_str(store, key, value);
    return e == ESP_OK ? nvs_commit(store) : e;
}
void sc_relay_off(void)
{
    gpio_set_level(GPIO_NUM_25, 0);
    gpio_config_t c = {.pin_bit_mask=1ULL << 25, .mode=GPIO_MODE_INPUT_OUTPUT,
                      .pull_down_en=GPIO_PULLDOWN_ENABLE};
    ESP_ERROR_CHECK(gpio_config(&c));
    ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_25, 0));
}
static bool field(cJSON *j, const char *name, char *dst, size_t cap)
{
    cJSON *v=cJSON_GetObjectItemCaseSensitive(j,name);
    if (!cJSON_IsString(v) || strlen(v->valuestring)>=cap) return false;
    strcpy(dst,v->valuestring);
    return true;
}
static bool token(const char *s)
{
    if (!*s) return false;
    for (;*s;s++) if (!isalnum((unsigned char)*s) && *s!='_' && *s!='-' && *s!='.') return false;
    return true;
}
bool sc_trusted_url(const char *url)
{
    size_t n=strlen(sc_config.trust_base);
    if (n<10 || strncmp(sc_config.trust_base,"https://",8) || sc_config.trust_base[n-1]!='/') return false;
    if (strncmp(url,sc_config.trust_base,n) || strlen(url)>=384) return false;
    /* Reject malformed URLs before passing them to SDK diagnostics, which may
     * print a bad URL. Capability tokens must never reach such parse-error logs. */
    for(const unsigned char *p=(const unsigned char *)url;*p;p++)
        if(!((*p>='a' && *p<='z') || (*p>='A' && *p<='Z') || (*p>='0' && *p<='9') ||
             *p=='-' || *p=='_' || *p=='.' || *p=='~' || *p==':' || *p=='/')) return false;
    const char *authority=sc_config.trust_base+8,*slash=strchr(authority,'/');
    if(!slash || slash==authority) return false;
    const char *colon=memchr(authority,':',(size_t)(slash-authority));
    const char *host_end=colon?colon:slash;
    if(host_end==authority) return false;
    for(const char *p=authority;p<host_end;p++)
        if(!isalnum((unsigned char)*p) && *p!='.' && *p!='-') return false;
    if(colon) {
        if(slash-colon<=1 || slash-colon>6) return false;
        unsigned port=0;
        for(const char *p=colon+1;p<slash;p++) {
            if(*p<'0' || *p>'9') return false;
            port=port*10+(unsigned)(*p-'0');
        }
        if(!port || port>65535) return false;
    }
    /* Fixed trusted prefix, no userinfo, query credentials, redirects or escapes. */
    return strpbrk(url,"\r\n\t @?#%\\") == NULL && strstr(url+8,"..") == NULL;
}
bool sc_private_broker(const char *uri)
{
    if(strncmp(uri,"mqtts://",8)) return false;
    const char *start=uri+8,*colon=strchr(start,':');
    size_t length=colon?(size_t)(colon-start):strlen(start);
    if(!length || length>=192 || strpbrk(start,"\r\n\t @?#/\\")) return false;
    char host[192];
    for(size_t i=0;i<length;i++) {
        unsigned char c=start[i];
        if(!isalnum(c) && c!='.' && c!='-') return false;
        host[i]=(char)tolower(c);
    }
    while(length && host[length-1]=='.') length--;
    host[length]=0;
    const char *public_hosts[]={"broker.hivemq.com","broker.emqx.io"};
    if(!length) return false;
    for(size_t i=0;i<sizeof(public_hosts)/sizeof(public_hosts[0]);i++) {
        size_t public_len=strlen(public_hosts[i]);
        if(!strcmp(host,public_hosts[i]) || (length>public_len &&
           host[length-public_len-1]=='.' && !strcmp(host+length-public_len,public_hosts[i]))) return false;
    }
    if(colon) {
        if(!colon[1] || strspn(colon+1,"0123456789")!=strlen(colon+1) || strlen(colon+1)>5) return false;
        unsigned long port=strtoul(colon+1,NULL,10);
        if(!port || port>65535) return false;
    }
    return true;
}
bool sc_ota_enabled(void)
{
    return storage_ready && !sc_config.mqtt_public_test_mode &&
           strlen(sc_config.key_hex)==64 && strspn(sc_config.key_hex,"0123456789abcdefABCDEF")==64 &&
           sc_private_broker(sc_config.broker) &&
           sc_config.mqtt_user[0] && sc_config.mqtt_password[0] &&
           sc_trusted_url(sc_config.trust_base);
}
bool sc_verify_hmac(const char *text, const char *hex)
{
    if (!sc_ota_enabled() || strlen(hex)!=64) return false;
    unsigned char key[32], expected[32], actual[32];
    for (int i=0;i<32;i++) {
        unsigned k,h;
        if (!isxdigit((unsigned char)sc_config.key_hex[2*i]) ||
            !isxdigit((unsigned char)sc_config.key_hex[2*i+1]) ||
            !isxdigit((unsigned char)hex[2*i]) || !isxdigit((unsigned char)hex[2*i+1])) return false;
        sscanf(sc_config.key_hex+2*i,"%2x",&k); sscanf(hex+2*i,"%2x",&h);
        key[i]=k; expected[i]=h;
    }
    int r=mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),key,32,
                          (const unsigned char *)text,strlen(text),actual);
    unsigned diff=0;
    for (int i=0;i<32;i++) diff|=expected[i]^actual[i];
    memset(key,0,sizeof(key));
    return r==0 && diff==0;
}
static bool version_parts(const char *s, unsigned v[3])
{
    char extra;
    return strlen(s)<32 && sscanf(s,"%u.%u.%u%c",&v[0],&v[1],&v[2],&extra)==3 &&
           v[0]<=65535 && v[1]<=65535 && v[2]<=65535 && strpbrk(s,"+- \n\r")==NULL;
}
bool sc_version_newer(const char *candidate, const char *current)
{
    unsigned a[3],b[3];
    if (!version_parts(candidate,a) || !version_parts(current,b)) return false;
    for (int i=0;i<3;i++) if (a[i]!=b[i]) return a[i]>b[i];
    return false;
}
esp_err_t sc_init(void)
{
    sc_relay_off();
    /* Never erase shared provisioning / OTA state automatically on NVS errors. */
    esp_err_t e=nvs_flash_init(); if(e!=ESP_OK) return e;
    e=nvs_open("sc",NVS_READWRITE,&store); if(e!=ESP_OK) return e;
    storage_ready=true;
    char *config=calloc(1,4096);
    if(!config) return ESP_ERR_NO_MEM;
    size_t n=4096;
    e=nvs_get_str(store,"cfg",config,&n);
    cJSON *j=e==ESP_OK?sc_parse_flat_json(config,4095):NULL;
    free(config);
    if(e!=ESP_OK) return e;
    const cJSON *mode=cJSON_GetObjectItemCaseSensitive(j,"MQTT_PUBLIC_TEST_MODE");
    sc_config.mqtt_public_test_mode=cJSON_IsTrue(mode);
    bool ok=j && (!mode || cJSON_IsBool(mode)) &&
        field(j,"device",sc_config.device,sizeof(sc_config.device)) && token(sc_config.device) &&
        field(j,"ssid",sc_config.ssid,sizeof(sc_config.ssid)) && sc_config.ssid[0] &&
        field(j,"password",sc_config.password,sizeof(sc_config.password)) &&
        field(j,"broker",sc_config.broker,sizeof(sc_config.broker)) &&
        field(j,"trust_base",sc_config.trust_base,sizeof(sc_config.trust_base));
    if(ok && !sc_config.mqtt_public_test_mode) {
        ok=field(j,"mqtt_user",sc_config.mqtt_user,sizeof(sc_config.mqtt_user)) &&
           field(j,"mqtt_password",sc_config.mqtt_password,sizeof(sc_config.mqtt_password)) &&
           field(j,"key_hex",sc_config.key_hex,sizeof(sc_config.key_hex)) && sc_ota_enabled();
    } else if(ok) {
        const char *alias=sc_config.device;
        ok=!strcmp(sc_config.broker,SC_PUBLIC_TEST_BROKER) && sc_trusted_url(sc_config.trust_base) &&
           strlen(alias)==24 && !strncmp(alias,"SC_TEST_",8) &&
           strspn(alias+8,"0123456789abcdef")==16;
    }
    cJSON_Delete(j);
    if(!ok) return ESP_ERR_INVALID_ARG;
    uint32_t bytes=0;
    e=esp_flash_get_physical_size(NULL,&bytes);
    if(e!=ESP_OK || bytes!=0x400000) return ESP_ERR_INVALID_SIZE;
    const esp_partition_t *f=esp_partition_find_first(0,ESP_PARTITION_SUBTYPE_APP_FACTORY,NULL);
    const esp_partition_t *a=esp_partition_find_first(0,ESP_PARTITION_SUBTYPE_APP_OTA_0,NULL);
    if(!f || !a || f->address!=0x10000 || f->size!=0x100000 ||
       a->address!=0x110000 || a->size!=0x2f0000) return ESP_ERR_INVALID_STATE;
    const char *labels[]={"nvs","otadata","phy_init"};
    const uint32_t offsets[]={0x9000,0xd000,0xf000},sizes[]={0x4000,0x2000,0x1000};
    for(int i=0;i<3;i++) {
        const esp_partition_t *p=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,ESP_PARTITION_SUBTYPE_ANY,labels[i]);
        if(!p || p->address!=offsets[i] || p->size!=sizes[i]) return ESP_ERR_INVALID_STATE;
        ESP_LOGI("MEM","Partition %s offset=0x%"PRIx32" size=%"PRIu32" B",p->label,p->address,p->size);
    }
    ESP_LOGI("MEM","Partition factory offset=0x10000 size=1048576 B; ota_0 offset=0x110000 size=3080192 B");
    sc_log("INFO","BOOT","Relay OFF; application starting");
    ESP_LOGI("MEM","Flash physical=%"PRIu32" B configured=4MB; reset_reason=%d",bytes,esp_reset_reason());
    return ESP_OK;
}
esp_err_t sc_parse_request(const char *json, sc_request_t *r)
{
    memset(r,0,sizeof(*r));
    if(!sc_ota_enabled()) return ESP_ERR_NOT_SUPPORTED;
    cJSON *j=sc_parse_flat_json(json,SC_MESSAGE_MAX-1); if(!j) return ESP_ERR_INVALID_ARG;
    char command[24],seq[16];
    bool ok=field(j,"device",r->device,sizeof(r->device)) &&
        field(j,"command",command,sizeof(command)) && !strcmp(command,"START_OTA") &&
        field(j,"request_id",r->request_id,sizeof(r->request_id)) &&
        field(j,"version",r->version,sizeof(r->version)) &&
        field(j,"manifest_url",r->manifest_url,sizeof(r->manifest_url)) &&
        field(j,"seq",seq,sizeof(seq)) && field(j,"signature",r->signature,sizeof(r->signature));
    cJSON_Delete(j);
    if(!ok || strcmp(r->device,sc_config.device) || !token(r->request_id) ||
        !token(r->version) || !sc_trusted_url(r->manifest_url)) return ESP_ERR_INVALID_ARG;
    char *end; errno=0; unsigned long value=strtoul(seq,&end,10);
    if(!seq[0] || strspn(seq,"0123456789")!=strlen(seq) || errno || *end || value==0 || value>UINT32_MAX) return ESP_ERR_INVALID_ARG;
    r->seq=value;
    char canonical[768];
    snprintf(canonical,sizeof(canonical),"SC-CMD-1\n%s\nSTART_OTA\n%s\n%s\n%s\n%"PRIu32,
             r->device,r->request_id,r->version,r->manifest_url,r->seq);
    return sc_verify_hmac(canonical,r->signature)?ESP_OK:ESP_ERR_INVALID_CRC;
}
esp_err_t sc_check_request(const sc_request_t *r)
{
    uint32_t last=0; esp_err_t e=nvs_get_u32(store,"seq",&last);
    if(e!=ESP_OK && e!=ESP_ERR_NVS_NOT_FOUND) return e;
    sc_request_t previous;
    if(sc_load_request(&previous)==ESP_OK && !strcmp(previous.request_id,r->request_id)) return ESP_ERR_INVALID_STATE;
    char current[32]="0.0.0"; size_t len=sizeof(current);
    e=nvs_get_str(store,"version",current,&len);
    if(e!=ESP_OK && e!=ESP_ERR_NVS_NOT_FOUND) return e;
#ifndef SC_RECOVERY
    snprintf(current,sizeof(current),"%s",esp_app_get_description()->version);
#endif
    if(r->seq<=last || !sc_version_newer(r->version,current)) return ESP_ERR_INVALID_STATE;
    return ESP_OK;
}
esp_err_t sc_accept_request(const sc_request_t *r)
{
    esp_err_t e=sc_check_request(r);
    if(e!=ESP_OK) return e;
    /* Persist replay floor first: a torn acceptance may consume a sequence, never replay it. */
    e=nvs_set_u32(store,"seq",r->seq); if(e==ESP_OK) e=nvs_commit(store);
    if(e!=ESP_OK) return e;
    e=nvs_set_blob(store,"request",r,sizeof(*r)); if(e==ESP_OK) e=nvs_commit(store);
    if(e==ESP_OK) e=nvs_set_u32(store,"failures",0);
    if(e==ESP_OK) e=nvs_set_str(store,"last_error","");
    if(e==ESP_OK) e=sc_set_state("pending");
    return e;
}
esp_err_t sc_load_request(sc_request_t *r)
{
    size_t n=sizeof(*r); esp_err_t e=nvs_get_blob(store,"request",r,&n);
    if(e!=ESP_OK || n!=sizeof(*r)) return ESP_ERR_NOT_FOUND;
    /* Stored record is never used as a URL without bounded-string validation. */
    if(!memchr(r->device,0,sizeof(r->device)) || !memchr(r->request_id,0,sizeof(r->request_id)) ||
       !memchr(r->version,0,sizeof(r->version)) || !memchr(r->manifest_url,0,sizeof(r->manifest_url)) ||
       !memchr(r->signature,0,sizeof(r->signature))) return ESP_ERR_INVALID_ARG;
    if(strcmp(r->device,sc_config.device) || !token(r->request_id) || !token(r->version) ||
       !sc_trusted_url(r->manifest_url)) return ESP_ERR_INVALID_ARG;
    char canonical[768];
    snprintf(canonical,sizeof(canonical),"SC-CMD-1\n%s\nSTART_OTA\n%s\n%s\n%s\n%"PRIu32,
             r->device,r->request_id,r->version,r->manifest_url,r->seq);
    return sc_verify_hmac(canonical,r->signature)?ESP_OK:ESP_ERR_INVALID_CRC;
}
esp_err_t sc_set_state(const char *state)
{
    if(!strcmp(state,"candidate")) {
        esp_err_t e=nvs_set_u32(store,"boots",0); if(e!=ESP_OK) return e;
    }
    return put_string("state",state);
}
esp_err_t sc_get_state(char *state,size_t n) { return nvs_get_str(store,"state",state,&n); }
esp_err_t sc_fail_update(const char *reason)
{
    if(!reason || strlen(reason)>=96) return ESP_ERR_INVALID_ARG;
    esp_err_t e=nvs_set_str(store,"last_error",reason);
    return e==ESP_OK?sc_set_state("failed"):e;
}
void sc_failure_reason(char *reason,size_t capacity)
{
    size_t n=capacity;
    if(nvs_get_str(store,"last_error",reason,&n)!=ESP_OK || !reason[0])
        snprintf(reason,capacity,"OTA_FAILED");
}
esp_err_t sc_go_recovery(void)
{
    sc_relay_off();
    const esp_partition_t *f=esp_partition_find_first(0,ESP_PARTITION_SUBTYPE_APP_FACTORY,NULL);
    if(!f) return ESP_ERR_NOT_FOUND;
    esp_err_t e=esp_ota_set_boot_partition(f);
    if(e!=ESP_OK) return e;
    const esp_partition_t *boot=esp_ota_get_boot_partition();
    return boot && boot->address==f->address ? ESP_OK:ESP_ERR_INVALID_STATE;
}
esp_err_t sc_boot_guard(void)
{
    char update_state[24]={0};
    sc_request_t pending_request;
    if(!sc_config.mqtt_public_test_mode && sc_get_state(update_state,sizeof(update_state))==ESP_OK && !strcmp(update_state,"pending") &&
       sc_load_request(&pending_request)==ESP_OK) {
        esp_err_t transfer=sc_go_recovery();
        if(transfer==ESP_OK) esp_restart();
        return transfer;
    }
    uint32_t attempts=0;
    esp_err_t e=nvs_get_u32(store,"boots",&attempts);
    if(e!=ESP_OK && e!=ESP_ERR_NVS_NOT_FOUND) return e;
    if(attempts>=3) {
        sc_request_t request;
        if(!sc_config.mqtt_public_test_mode && sc_load_request(&request)==ESP_OK) {
            e=sc_set_state("pending"); if(e!=ESP_OK) return e;
        }
        e=sc_go_recovery(); if(e==ESP_OK) esp_restart(); return e;
    }
    e=nvs_set_u32(store,"boots",attempts+1);
    return e==ESP_OK?nvs_commit(store):e;
}
esp_err_t sc_recovery_entry(void)
{
    char state[24]={0};
    if(sc_get_state(state,sizeof(state))!=ESP_OK || strcmp(state,"candidate")) return ESP_OK;
    uint32_t failures=0;
    esp_err_t e=nvs_get_u32(store,"failures",&failures);
    if(e!=ESP_OK && e!=ESP_ERR_NVS_NOT_FOUND) return e;
    e=nvs_set_u32(store,"failures",failures+1); if(e!=ESP_OK) return e;
    return failures+1>=3?sc_fail_update("OTA_BOOT_VALIDATION_FAILED"):sc_set_state("pending");
}
esp_err_t sc_confirm_healthy(void)
{
    char update_state[24]={0};
    if(!sc_config.mqtt_public_test_mode && sc_get_state(update_state,sizeof(update_state))==ESP_OK &&
       !strcmp(update_state,"candidate")) {
        sc_request_t r;
        if(sc_load_request(&r)!=ESP_OK || strcmp(r.version,esp_app_get_description()->version)) return ESP_ERR_INVALID_VERSION;
    }
    const esp_partition_t *p=esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    if(p && esp_ota_get_state_partition(p,&state)==ESP_OK && state==ESP_OTA_IMG_PENDING_VERIFY) {
        esp_err_t e=esp_ota_mark_app_valid_cancel_rollback(); if(e!=ESP_OK) return e;
    }
    esp_err_t e=nvs_set_u32(store,"boots",0);
    if(e==ESP_OK) e=nvs_set_str(store,"version",esp_app_get_description()->version);
    if(e==ESP_OK) e=nvs_commit(store);
    return e;
}
void sc_memory_log(void)
{
    unsigned caps=MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT;
    unsigned free=heap_caps_get_free_size(caps);
    ESP_LOGI("MEM","RAM internal: free=%u B, minimum=%u B, largest block=%u B, task=%s stack_watermark=%u B dropped=%"PRIu32,
        free,(unsigned)heap_caps_get_minimum_free_size(caps),(unsigned)heap_caps_get_largest_free_block(caps),
        pcTaskGetName(NULL),(unsigned)uxTaskGetStackHighWaterMark(NULL),sc_dropped_messages());
    if(free<20000 || heap_caps_get_largest_free_block(caps)<8192)
        sc_log("WARN","HEAP_LOW","Internal RAM pressure");
}
