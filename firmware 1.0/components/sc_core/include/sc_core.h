#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "cJSON.h"
#include "sc_http_policy.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define SC_BASE "iot/charge_station/v1/"
#define SC_PUBLIC_TEST_BROKER "mqtts://broker.emqx.io:8883"
#define SC_PUBLIC_TEST_BASE "iot/charge_station/test/"
#define SC_MESSAGE_MAX 1536
typedef struct {
    char device[32], ssid[33], password[65];
    char broker[192], mqtt_user[64], mqtt_password[128];
    char trust_base[192], key_hex[65];
    bool mqtt_public_test_mode, mqtt_public_signed_mode;
} sc_config_t;
typedef struct {
    char device[32], request_id[64], version[32], manifest_url[384];
    uint32_t seq;
    char signature[65];
} sc_request_t;
typedef struct { uint32_t network_epoch; bool ota; char data[SC_MESSAGE_MAX]; } sc_command_t;
extern sc_config_t sc_config;
cJSON *sc_parse_flat_json(const char *text, size_t max_length);
esp_err_t sc_init(void);
void sc_relay_off(void);
bool sc_ota_enabled(void);
esp_err_t sc_commit_order_sequence(uint32_t sequence);
bool sc_private_broker(const char *uri);
bool sc_trusted_url(const char *url);
bool sc_version_newer(const char *candidate, const char *current);
esp_err_t sc_parse_request(const char *json, sc_request_t *out);
esp_err_t sc_accept_request(const sc_request_t *request);
esp_err_t sc_check_request(const sc_request_t *request);
esp_err_t sc_load_request(sc_request_t *request);
esp_err_t sc_set_state(const char *state);
esp_err_t sc_fail_update(const char *reason);
void sc_failure_reason(char *reason, size_t capacity);
esp_err_t sc_get_state(char *state, size_t size);
esp_err_t sc_go_recovery(void);
esp_err_t sc_boot_guard(void);
esp_err_t sc_recovery_entry(void);
esp_err_t sc_confirm_healthy(void);
esp_err_t sc_install(const sc_request_t *request);
#define SC_OTA_HTTP_ERROR_BASE 0x7c00
#define SC_OTA_ERR_TLS_CONNECT 0x7d00
#define SC_OTA_ERR_DOWNLOAD_INTERRUPTED 0x7d01
const char *sc_update_error_name(esp_err_t error);
bool sc_update_retryable(esp_err_t error);
bool sc_verify_hmac(const char *text, const char *hex);
void sc_network_start(void (*wifi_changed)(bool));
bool sc_network_ready(void);
uint32_t sc_network_epoch(void);
bool sc_download_ready(void);
bool sc_receive(sc_command_t *command);
bool sc_publish(const char *topic, const char *json, bool reliable);
void sc_log(const char *level, const char *event, const char *message);
void sc_status(const sc_request_t *request, const char *status, int progress);
void sc_status_error(const sc_request_t *request, const char *status, int progress, const char *error);
void sc_memory_log(void);
void sc_wait_outbox(unsigned timeout_ms);
uint32_t sc_dropped_messages(void);
