#include "wifi_mqtt.h"
#include "display_lvgl.h"
#include "sc_core.h"
void wifi_init_sta(void) { sc_network_start(display_set_wifi_connected); }
