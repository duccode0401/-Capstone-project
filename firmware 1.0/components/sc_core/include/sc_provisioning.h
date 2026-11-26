#pragma once

#include <stdbool.h>
#include "esp_err.h"

esp_err_t provisioning_init(void (*show_credentials)(const char *,const char *));
void provisioning_start(void);
void provisioning_wifi_disconnected(void);
void provisioning_wifi_connected(void);
bool provisioning_active(void);
