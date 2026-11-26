idf_component_register(SRCS "sc_core.c" "sc_network.c" "sc_update.c" "sc_http_policy.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_wifi esp_event esp_netif mqtt nvs_flash json app_update
             esp_http_client mbedtls esp_timer esp_driver_gpio esp_driver_uart spi_flash esp_app_format)
