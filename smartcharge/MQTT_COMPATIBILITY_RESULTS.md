# Kết quả tương thích MQTT Web và Firmware

Ngày kiểm tra: 2026-11-26.

## Giao thức đã đồng bộ

- Broker Public Test: `broker.emqx.io:8883`, TLS xác minh CA và hostname, không username/password.
- Device ID logic: `ESP32_001` trong trường `logical_device`.
- Routing alias: `SC_TEST_<16 ký tự hex thường>` trong trường `device` và trong topic.
- Topic gửi: `iot/charge_station/test/<alias>/telemetry`, `/logs`, `/heartbeat`.
- Topic Application ACK: `iot/charge_station/test/<alias>/ack`.
- ACK chỉ được gửi sau validate và database commit; duplicate đã thành công nhận lại ACK mà không lặp nghiệp vụ.
- Namespace Public Test không subscribe lệnh sạc, thanh toán hoặc OTA. Firmware không bật relay từ lệnh chưa xác thực.

## Kết quả kiểm thử

- Django unit test: 26 test PASS.
- Django system check: PASS.
- Live EMQX TLS: PASS với thiết bị mô phỏng; telemetry, logs và heartbeat đều được ghi database và nhận ACK `SUCCESS`.
- Allowlist log đã đối chiếu với toàn bộ event `sc_log()` của firmware hiện tại.
- Firmware ESP-IDF 5.5.1: build/verify/unit test/size PASS; `smartcharge.bin` 1.496.720 byte, app partition `0x3F0000`, còn 2.632.048 byte.

Không flash ESP32, không erase Flash/NVS và chưa tuyên bố PASS cho Wi-Fi Setup, LCD/LVGL, ADC, cảm biến hay relay trên phần cứng thật.
