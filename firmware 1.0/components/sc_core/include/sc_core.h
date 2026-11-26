#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define SC_BASE "iot/charge_station/v1/"
#define SC_PUBLIC_PREFIX "iot/charge_station/test/"
#define SC_MQTT_BROKER "mqtts://broker.emqx.io:8883"
#define SC_MESSAGE_MAX 1536
typedef struct {
    char device[32], mqtt_alias[25], ssid[33], password[65];
    char key_hex[65];
    bool command_auth_ready;
} sc_config_t;
typedef struct { uint32_t network_epoch; char data[SC_MESSAGE_MAX]; } sc_command_t;
extern sc_config_t sc_config;
const char *sc_logical_device(void);
const char *sc_routing_device(void);
bool sc_serial_debug(void);
cJSON *sc_parse_flat_json(const char *text, size_t max_length);
esp_err_t sc_init(void);
void sc_relay_off(void);
bool sc_command_auth_ready(void);
esp_err_t sc_commit_order_sequence(uint32_t sequence);
bool sc_verify_hmac(const char *text, const char *hex);
void sc_network_start(void (*wifi_changed)(bool),
                      void (*show_setup)(const char *,const char *));
bool sc_network_ready(void);
void sc_set_health(bool sensor_ok, bool display_ok);
void sc_network_start_headless(void);
uint32_t sc_network_epoch(void);
bool sc_receive(sc_command_t *command);
bool sc_publish(const char *topic, const char *json, bool reliable);
void sc_log(const char *level, const char *event, const char *message);
void sc_memory_log(void);
void sc_wait_outbox(unsigned timeout_ms);
uint32_t sc_dropped_messages(void);
