# Kết quả kiểm thử firmware một ứng dụng

Ngày kiểm tra: 2026-11-26. Môi trường ESP-IDF 5.5.1 trên Windows. Không flash board và không tuyên bố đã kiểm thử phần cứng thật.

## Đã xác minh

- `idf.py -B build-smartcharge fullclean build`: PASS.
- `tools/verify_builds.py`: PASS; ESP32, flash 4 MB, app factory tại `0x10000`.
- `python -m unittest discover -s tests -v`: PASS cho test Python/NVS/protocol. Các harness C host được skip vì máy không có compiler C native; source C vẫn được Xtensa compiler build thành công.
- ELF không có symbol OTA download/write/boot switching (`esp_ota_begin`, `esp_ota_write`, `esp_ota_set_boot_partition`, `esp_https_ota*`). IDF vẫn link `esp_ota_get_running_partition` nội bộ vì `esp_partition` dùng nó để chặn ghi đè app đang chạy; firmware không cung cấp luồng OTA.
- `smartcharge.bin`: 1.496.720 byte (`0x16D690`). Partition app: 4.128.768 byte (`0x3F0000`), còn 2.632.048 byte (64%).
- RAM tĩnh: DRAM 117.108/180.736 byte (64,80%), còn 63.628 byte. IRAM 112.031/131.072 byte (85,47%), còn 19.041 byte.
- Bootloader: `0x6680`, còn `0x980` byte.
- Layout giữ NVS `0x9000/0x4000` và PHY `0xF000/0x1000`; không còn otadata, Recovery hoặc OTA slot.
- Tool migration luôn backup đủ 4 MB trước khi ghi và chỉ thay NVS nếu người vận hành truyền `--nvs`.
- MQTT Public Test dùng `iot/charge_station/test/<SC_TEST_alias>/{telemetry,logs,heartbeat,status}` và nhận ACK tại `.../ack`; TLS xác minh CA/hostname với `broker.emqx.io:8883`.
- Alias `SC_TEST_<16 hex>` được tạo một lần và lưu riêng trong NVS key `mqtt_alias`; Device ID logic vẫn là `ESP32_001` trong `logical_device`.
- Toàn bộ event phát từ `sc_log()` hiện nằm trong allowlist Web. Lệnh relay trên topic `v1/orders` vẫn bắt buộc HMAC, device, nonce/sequence và hạn dùng; không có lệnh điều khiển trên namespace Public Test.

## Cần kiểm thử trên ESP32 thật

- Setup Mode bằng điện thoại, timeout và mật khẩu riêng.
- Wi-Fi sai không ghi NVS; Wi-Fi đúng tồn tại qua reboot; đổi AP không cần nạp lại firmware.
- Mất/khôi phục Wi-Fi và MQTT, TLS hostname/CA, telemetry/log/ACK với Django.
- Heap thực tế và high-water mark của từng task sau nhiều giờ reconnect.
- Relay OFF lúc boot, Setup Mode, mất mạng, lỗi ADC và watchdog.
- ADC DMA, RMS, V/I/P/Wh, LCD/LVGL và phiên sạc với tải thử an toàn.
