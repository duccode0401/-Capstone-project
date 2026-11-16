# See the build system documentation in IDF programming guide
# for more information about component CMakeLists.txt files.

idf_component_register(
    SRCS
        "main.c"
        "power_meter.c"
        "display_lvgl.c"
        "screens/charge_test_screen.c"
        "wifi_mqtt.c"
    INCLUDE_DIRS
        "."
        "screens"
    REQUIRES
        mqtt
        lvgl__lvgl
    PRIV_REQUIRES
        esp_adc
        esp_driver_gpio
        esp_driver_ledc
        esp_driver_spi
        esp_lcd
        esp_timer
        esp_wifi
        esp_event
        esp_netif
        mbedtls
        nvs_flash
        espressif__esp_lcd_ili9341
)
