#include "sc_core.h"

#include <ctype.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "mbedtls/md.h"
#include "nvs.h"
#include "sdkconfig.h"

#if !CONFIG_ESPTOOLPY_FLASHSIZE_4MB || !CONFIG_PARTITION_TABLE_CUSTOM
#error "SmartCharge requires the 4 MB custom single-application partition table"
#endif

sc_config_t sc_config;
static nvs_handle_t store;

const char *sc_logical_device(void) { return sc_config.device; }
const char *sc_routing_device(void) { return sc_config.mqtt_alias; }

cJSON *sc_parse_flat_json(const char *text,size_t max_length)
{
    if(!text || strnlen(text,max_length+1)>max_length) return NULL;
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
    cJSON *json=cJSON_ParseWithOpts(text,NULL,true);
    if(!cJSON_IsObject(json)) { cJSON_Delete(json); return NULL; }
    for(cJSON *a=json->child;a;a=a->next)
        for(cJSON *b=a->next;b;b=b->next)
            if(!strcmp(a->string,b->string)) { cJSON_Delete(json); return NULL; }
    return json;
}

void sc_relay_off(void)
{
    gpio_set_level(GPIO_NUM_25,0);
    gpio_config_t config={.pin_bit_mask=1ULL<<GPIO_NUM_25,.mode=GPIO_MODE_INPUT_OUTPUT,
                          .pull_down_en=GPIO_PULLDOWN_ENABLE};
    ESP_ERROR_CHECK(gpio_config(&config));
    ESP_ERROR_CHECK(gpio_set_level(GPIO_NUM_25,0));
}

bool sc_command_auth_ready(void) { return sc_config.command_auth_ready; }

esp_err_t sc_commit_order_sequence(uint32_t sequence)
{
    if(!sc_command_auth_ready() || !sequence) return ESP_ERR_INVALID_STATE;
    uint32_t previous=0;
    esp_err_t error=nvs_get_u32(store,"order_seq",&previous);
    if(error!=ESP_OK && error!=ESP_ERR_NVS_NOT_FOUND) return error;
    if(sequence<=previous) return ESP_ERR_INVALID_STATE;
    error=nvs_set_u32(store,"order_seq",sequence);
    return error==ESP_OK?nvs_commit(store):error;
}

bool sc_verify_hmac(const char *text,const char *hex)
{
    if(!sc_command_auth_ready() || !text || !hex || strlen(hex)!=64) return false;
    unsigned char key[32],expected[32],actual[32];
    for(int i=0;i<32;i++) {
        unsigned k,h;
        if(!isxdigit((unsigned char)sc_config.key_hex[2*i]) ||
           !isxdigit((unsigned char)sc_config.key_hex[2*i+1]) ||
           !isxdigit((unsigned char)hex[2*i]) || !isxdigit((unsigned char)hex[2*i+1])) return false;
        sscanf(sc_config.key_hex+2*i,"%2x",&k);
        sscanf(hex+2*i,"%2x",&h);
        key[i]=(unsigned char)k;
        expected[i]=(unsigned char)h;
    }
    int result=mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),key,sizeof(key),
                               (const unsigned char *)text,strlen(text),actual);
    unsigned difference=0;
    for(int i=0;i<32;i++) difference|=expected[i]^actual[i];
    memset(key,0,sizeof(key));
    return result==0 && difference==0;
}

esp_err_t sc_init(void)
{
    sc_relay_off();
    esp_err_t error=nvs_open("sc",NVS_READWRITE,&store);
    if(error!=ESP_OK) return error;
    uint32_t bytes=0;
    error=esp_flash_get_physical_size(NULL,&bytes);
    if(error!=ESP_OK || bytes!=0x400000) return ESP_ERR_INVALID_SIZE;
    const esp_partition_t *nvs=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,
        ESP_PARTITION_SUBTYPE_DATA_NVS,"nvs");
    const esp_partition_t *phy=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,
        ESP_PARTITION_SUBTYPE_DATA_PHY,"phy_init");
    const esp_partition_t *app=esp_partition_find_first(ESP_PARTITION_TYPE_APP,
        ESP_PARTITION_SUBTYPE_APP_FACTORY,"factory");
    if(!nvs || nvs->address!=0x9000 || nvs->size!=0x4000 ||
       !phy || phy->address!=0xf000 || phy->size!=0x1000 ||
       !app || app->address!=0x10000 || app->size!=0x3f0000) return ESP_ERR_INVALID_STATE;
    ESP_LOGI("MEM","Flash=4MB NVS=0x9000/0x4000 PHY=0xF000/0x1000 APP=0x10000/0x3F0000");
    ESP_LOGI("BOOT","Single application layout; OTA and Recovery disabled");
    return ESP_OK;
}

void sc_memory_log(void)
{
    unsigned caps=MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT;
    unsigned free=heap_caps_get_free_size(caps);
    if(sc_serial_debug()) ESP_LOGI("MEM","RAM free=%u B minimum=%u B largest=%u B task=%s stack=%u B dropped=%"PRIu32,
        free,(unsigned)heap_caps_get_minimum_free_size(caps),
        (unsigned)heap_caps_get_largest_free_block(caps),pcTaskGetName(NULL),
        (unsigned)uxTaskGetStackHighWaterMark(NULL),sc_dropped_messages());
    if(free<20000 || heap_caps_get_largest_free_block(caps)<8192)
        sc_log("WARNING","HEAP_LOW","Internal RAM pressure");
}
