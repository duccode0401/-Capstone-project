# Build và kiểm thử SmartCharge trên Windows

## 1. Môi trường và build

Cài ESP-IDF **5.5.1** bằng Espressif Installation Manager. Mở ESP-IDF PowerShell 5.5.1, dùng đúng thư mục `firmware 1.0`:

~~~powershell
Set-Location 'D:\-Capstone-project-main\firmware 1.0'
idf.py --version
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota menuconfig
~~~

Trong menu **Component config → SmartCharge**, bật `HEADLESS_TEST_MODE` cho board trần chỉ cắm USB. Tắt mục này khi dùng LCD và cảm biến thật. Lưu menuconfig rồi chạy:

~~~powershell
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota build
Push-Location recovery
idf.py -B build-recovery -D SDKCONFIG=sdkconfig.recovery build
Pop-Location
python tools/verify_builds.py
python -m unittest discover -s tests -v
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota size
~~~

Headless chỉ xác nhận boot khi NVS Public Test đã qua bootstrap vật lý. Firmware không khởi tạo ADC/LVGL, không tạo số đo giả và giữ GPIO25 OFF. Không phân phối binary headless qua OTA production.

## 2. Provision và nạp USB lần đầu

**Ngắt tải điện vật lý trước khi nạp.** Xác nhận cổng COM trong Device Manager. Dùng Public Test cho board trần:

~~~powershell
New-Item -ItemType Directory -Force provisioning | Out-Null
Copy-Item provision.public-test.example.json provisioning/public-test.json
notepad provisioning/public-test.json
~~~

Điền Wi-Fi 2.4 GHz, giữ EMQX TLS và để trống MQTT credentials. Lưu file rồi chạy:

~~~powershell
python tools/provision.py --config provisioning/public-test.json --out provisioning/public-test --bootstrap
python tools/initial_flash.py --port COM3 --nvs provisioning/public-test/nvs.bin --backup "provisioning/public-test/flash-before-$(Get-Date -Format yyyyMMdd-HHmmss).bin"
~~~

Trong Secure mode, lấy `provision.example.json` làm mẫu, điền broker MQTT TLS riêng, user/password thiết bị và HTTPS trust base. Dùng cùng lệnh provision/initial flash với đường dẫn file tương ứng. Đồng bộ HMAC riêng của thiết bị với backend theo [giao thức OTA](OTA_MQTT_PROTOCOL.md). Giữ cả NVS, khóa bí mật và backup trên máy.

Installer xác minh flash 4 MB và sao lưu trước khi ghi. Recovery chạy ở `0x10000`, SmartCharge ở `0x110000`. Nếu backup dừng giữa chừng, thử cáp USB dữ liệu khác, cắm trực tiếp vào PC, đóng app đang giữ COM và dùng đường dẫn backup mới.

## 3. Cập nhật board đã provision

Với board đã boot Main ổn định, cùng layout và cùng chế độ hardware, USB `app-flash` chỉ cập nhật ảnh Main và giữ NVS. Kiểm tra offset trước khi chạy:

~~~powershell
python tools/verify_builds.py
Get-Content build-smartcharge/app-flash_args
# Phải thấy 0x110000 smartcharge.bin
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -p COM3 app-flash
~~~

Với production, dùng quy trình OTA hiện có để cập nhật qua Recovery. Khi build bản phát hành OTA, thêm `-D PROJECT_VER=X.Y.Z` vào lệnh build, với version cao hơn bản đang chạy. Nếu board đang nằm ở Recovery, `app-flash` không tự chọn lại slot Main. Dùng quy trình nạp lần đầu có backup khi cần bootstrap lại.

## 4. Serial Monitor, heartbeat và ACK

~~~powershell
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -p COM3 monitor --no-reset
~~~

UART chạy ở 115200. Thoát monitor bằng **Ctrl+]**. Sau khi nạp, để Main có thời gian hoàn tất kiểm tra boot trước khi bấm EN.

- `QUEUED` báo hàng đợi firmware đã nhận bản tin. Dòng `TX` in JSON thật của bản tin telemetry, heartbeat hoặc sự kiện.
- `PUBLISH_SUBMITTED` báo SDK đã nhận bản tin vào MQTT outbox. `BROKER_ACK` báo broker đã gửi PUBACK. `SERVER_SUCCESS` chỉ xuất hiện khi firmware nhận ACK ứng dụng hợp lệ.
- Heartbeat khoảng mỗi 10 giây khi có MQTT, kể cả khi tắt ADC/LCD. `HEADLESS` báo cảm biến và màn hình `DISABLED`.
- Nếu nhận PUBACK mà không có phản hồi ứng dụng, firmware thử lại sau khoảng 5 giây, tối đa ba lần, giữ nguyên ID và payload. `FAILED_NO_ACK` chỉ báo chưa nhận được ACK.
- `SERVER_FAILED` có mã lỗi. Firmware chỉ retry khi `retryable=true`. Các dòng `WAIT_DISCONNECTED`, `WAIT_OUTBOX_FULL` và `FAILED_QUEUE_FULL` giúp phân biệt lỗi vận chuyển.
- Nếu chờ MQTT, kiểm tra Wi-Fi, DNS và dòng SNTP. TLS cần đồng hồ đúng và chứng chỉ hợp lệ. So khớp alias `SC_TEST_…` giữa provisioning và subscriber.

DONE và OTA status chỉ retry ở cấp ứng dụng sau khi backend Secure trả được ACK v2. Quy tắc này tránh lặp thao tác trên Web cũ chưa lưu receipt.

Firmware phát telemetry, logs, heartbeat và status bằng schema v2, giữ trường cũ. Secure dùng `iot/charge_station/v1/ack/<device>`, Public Test dùng `iot/charge_station/test/<device>/ack`. ACK yêu cầu `protocol_version: 2`, `device`, `message_id`, `message_type`, `status` là `SUCCESS` hoặc `FAILED`, `error_code`, `retryable` và timestamp UTC của server. Backend phải gửi QoS 1, retain=false, chỉ gửi SUCCESS sau commit và lưu receipt theo Device ID + Message ID trước khi xử lý lại retry. Không cấp quyền điều khiển trên broker public. ACK public chỉ để chẩn đoán.

Django hiện tại vẫn ingest telemetry/logs cũ nhưng chưa gửi ACK v2 hoặc xử lý heartbeat. Vì vậy có thể thấy dữ liệu trên Web cùng lúc với `FAILED_NO_ACK` qua USB. Kiểm thử firmware host mô phỏng cả PUBACK, SERVER_ACK và trường hợp backend im lặng. Dùng tích hợp test riêng hoặc Web đã hỗ trợ ACK v2 để kiểm tra end-to-end. Không dùng subscriber Web cũ để kiểm thử phiên sạc thật với cơ chế retry v2.
