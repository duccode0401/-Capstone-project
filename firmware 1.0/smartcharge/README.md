# SmartCharge Main Firmware 2.0

Một application duy nhất gồm các module:

- `sc_storage`: NVS, migration cấu hình cũ và ghi Wi-Fi sau khi thử thành công.
- `sc_provisioning`: SoftAP + HTTP tại `192.168.4.1`, scan/chọn Wi-Fi, timeout và mật khẩu Setup.
- `sc_network`: Wi-Fi event, MQTT TLS, queue hữu hạn, heartbeat/log, QoS 1 và Application ACK.
- `signed_order`: HMAC, device/port/session/expiry/sequence chống replay.
- `main`, `power_meter`, `display_lvgl`: logic sạc, ADC DMA, RMS, V/I/P/Wh, GPIO/LCD/LVGL hiện có.

OTA Manager, MQTT OTA command, HTTPS firmware download, boot partition switching và Recovery đã bị loại bỏ. Xem [FIRMWARE_BUILD_GUIDE.md](../FIRMWARE_BUILD_GUIDE.md) để build và migration an toàn.
