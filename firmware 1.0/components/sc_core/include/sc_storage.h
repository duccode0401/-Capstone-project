#pragma once

#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

esp_err_t storage_init(void);
bool storage_has_wifi(void);
esp_err_t storage_save_wifi(const char *ssid,const char *password);
const char *storage_setup_password(void);
