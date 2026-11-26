idf_component_register(SRCS "sc_core.c" "sc_network.c" "sc_storage.c" "sc_provisioning.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_wifi esp_event esp_netif esp_http_server mqtt nvs_flash json
             mbedtls esp_timer esp_driver_gpio esp_driver_uart spi_flash esp_app_format)
