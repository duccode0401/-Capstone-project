#include "sc_storage.h"

#include <ctype.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_random.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sc_core.h"

static nvs_handle_t store;
static char setup_password[17];

static bool valid_device(const char *value)
{
    size_t length=strlen(value);
    if(!length || length>=sizeof(sc_config.device)) return false;
    for(size_t i=0;i<length;i++)
        if(!isalnum((unsigned char)value[i]) && value[i]!='_' && value[i]!='-' && value[i]!='.') return false;
    return true;
}

static bool valid_alias(const char *value)
{
    return value && strlen(value)==24 && !strncmp(value,"SC_TEST_",8) &&
        strspn(value+8,"0123456789abcdef")==16;
}

static bool json_string(cJSON *json,const char *key,char *output,size_t capacity)
{
    cJSON *value=cJSON_GetObjectItemCaseSensitive(json,key);
    if(!cJSON_IsString(value) || strlen(value->valuestring)>=capacity) return false;
    strcpy(output,value->valuestring);
    return true;
}

static esp_err_t load_string(const char *key,char *output,size_t capacity)
{
    size_t size=capacity;
    esp_err_t error=nvs_get_str(store,key,output,&size);
    if(error!=ESP_OK) output[0]=0;
    return error;
}

static void create_setup_password(void)
{
    static const char alphabet[]="ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
    for(size_t i=0;i<12;i++) setup_password[i]=alphabet[esp_random()%(sizeof(alphabet)-1)];
    setup_password[12]=0;
}

static void create_mqtt_alias(void)
{
    snprintf(sc_config.mqtt_alias,sizeof(sc_config.mqtt_alias),"SC_TEST_%08"PRIx32"%08"PRIx32,
             esp_random(),esp_random());
}

esp_err_t storage_init(void)
{
    esp_err_t error=nvs_flash_init();
    if(error!=ESP_OK) {
        ESP_LOGE("STORAGE","NVS init failed without erase: %s",esp_err_to_name(error));
        return error;
    }
    error=nvs_open("sc",NVS_READWRITE,&store);
    if(error!=ESP_OK) return error;
    strcpy(sc_config.device,"ESP32_001");

    char legacy[4096]={0};
    size_t size=sizeof(legacy);
    if(nvs_get_str(store,"cfg",legacy,&size)==ESP_OK) {
        cJSON *json=sc_parse_flat_json(legacy,sizeof(legacy)-1);
        char candidate[sizeof(sc_config.device)]={0};
        if(json) {
            if(json_string(json,"device",candidate,sizeof(candidate)) && valid_device(candidate))
                strcpy(sc_config.device,candidate);
            json_string(json,"mqtt_alias",sc_config.mqtt_alias,sizeof(sc_config.mqtt_alias));
            json_string(json,"ssid",sc_config.ssid,sizeof(sc_config.ssid));
            json_string(json,"password",sc_config.password,sizeof(sc_config.password));
            json_string(json,"key_hex",sc_config.key_hex,sizeof(sc_config.key_hex));
            cJSON_Delete(json);
        }
    }
    char saved_ssid[sizeof(sc_config.ssid)]={0},saved_password[sizeof(sc_config.password)]={0};
    if(load_string("wifi_ssid",saved_ssid,sizeof(saved_ssid))==ESP_OK) {
        strcpy(sc_config.ssid,saved_ssid);
        if(load_string("wifi_pass",saved_password,sizeof(saved_password))==ESP_OK)
            strcpy(sc_config.password,saved_password);
        else sc_config.password[0]=0;
    }
    char saved_alias[sizeof(sc_config.mqtt_alias)]={0};
    if(load_string("mqtt_alias",saved_alias,sizeof(saved_alias))==ESP_OK && valid_alias(saved_alias))
        strcpy(sc_config.mqtt_alias,saved_alias);
    if(!valid_alias(sc_config.mqtt_alias)) create_mqtt_alias();
    if(strcmp(saved_alias,sc_config.mqtt_alias)) {
        error=nvs_set_str(store,"mqtt_alias",sc_config.mqtt_alias);
        if(error==ESP_OK) error=nvs_commit(store);
        if(error!=ESP_OK) return error;
        ESP_LOGI("STORAGE","Generated and persisted a public MQTT routing alias");
    }
    sc_config.command_auth_ready=strlen(sc_config.key_hex)==64 &&
        strspn(sc_config.key_hex,"0123456789abcdefABCDEF")==64;

    if(load_string("setup_pass",setup_password,sizeof(setup_password))!=ESP_OK || strlen(setup_password)<8) {
        create_setup_password();
        error=nvs_set_str(store,"setup_pass",setup_password);
        if(error==ESP_OK) error=nvs_commit(store);
        if(error!=ESP_OK) return error;
        ESP_LOGI("STORAGE","Generated a dedicated Setup Mode password");
    }
    ESP_LOGI("STORAGE","NVS loaded; Wi-Fi=%s command_auth=%s alias=%s",
             sc_config.ssid[0]?"configured":"missing",sc_config.command_auth_ready?"ready":"disabled",
             sc_config.mqtt_alias);
    return ESP_OK;
}

bool storage_has_wifi(void) { return sc_config.ssid[0]!=0; }

esp_err_t storage_save_wifi(const char *ssid,const char *password)
{
    if(!ssid || !password || !ssid[0] || strlen(ssid)>=sizeof(sc_config.ssid) ||
       strlen(password)>=sizeof(sc_config.password)) return ESP_ERR_INVALID_ARG;
    esp_err_t error=nvs_set_str(store,"wifi_ssid",ssid);
    if(error==ESP_OK) error=nvs_set_str(store,"wifi_pass",password);
    if(error==ESP_OK) error=nvs_commit(store);
    if(error==ESP_OK) {
        strcpy(sc_config.ssid,ssid);
        strcpy(sc_config.password,password);
    }
    return error;
}

const char *storage_setup_password(void) { return setup_password; }
