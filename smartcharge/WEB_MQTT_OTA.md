# SmartCharge Web và MQTT

## Chạy Django trên Windows

Mở PowerShell trong `smartcharge/`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
```

Lưu giá trị vừa tạo vào `DJANGO_SECRET_KEY` trong `.env` cục bộ. Với EMQX chỉ đọc, đặt `MQTT_PUBLIC_TEST_MODE=true`, `MQTT_BROKER=broker.emqx.io`, `MQTT_PORT=8883` và để trống credentials. Bỏ dòng `MQTT_PUBLIC_TEST_DEVICE` cũng được. Khi để trống, subscriber đọc hai topic `iot/charge_station/v1/{telemetry,logs}` và chỉ nhận payload của `ESP32_001`. Firmware 1.0 ở chế độ bench chỉ phát vào `iot/charge_station/test/<alias>/...`. Để đọc từ chính bản firmware đó, dùng alias in ra khi provision theo [hướng dẫn bench](../firmware%201.0/PUBLIC_MQTT_TEST.md). Hai cấu hình topic này cùng dùng cơ sở dữ liệu thử nghiệm riêng.

Terminal thứ nhất:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --insecure
```

Dùng `--insecure` chỉ cho việc phục vụ CSS/JS từ Django runserver cục bộ. Giữ `DJANGO_DEBUG=false` khi chuyển tiếp ngrok.

Terminal thứ hai trong cùng thư mục:

```powershell
.\.venv\Scripts\python.exe mqtt_subscriber.py
```

Đăng nhập tại `http://127.0.0.1:8000/admin/`, rồi mở `/dashboard/devices/` và `/dashboard/logs/`. Hai API `/api/devices/status/` và `/api/devices/logs/` tiếp tục dùng quyền staff. Firmware 1.0 gửi V/I/P/Wh và các mã sự kiện. Các trường tùy chọn `heap_free_bytes`, `rssi_dbm`, `firmware_version` chỉ có giá trị khi firmware gửi chúng qua MQTT. Heap ghi trực tiếp trên serial sẽ không xuất hiện trên Web. Phiên bản do telemetry báo được hiển thị riêng với phiên bản đã xác nhận qua OTA.

## SePay và OTA với Firmware 1.0 hiện tại

Luồng điều khiển hiện tại dùng chế độ tương thích broker TLS riêng và ACL. Đặt `MQTT_PUBLIC_TEST_MODE=false`, cấu hình broker cùng MQTT credentials, và provision Firmware 1.0 với cùng broker. Không dùng EMQX công cộng làm broker điều khiển. Cho phép backend publish `orders` và `ota/command`, và chỉ cho thiết bị được xác thực publish `status`, `telemetry`, `logs` và `ota/status`. Tiền tố của các topic là `iot/charge_station/v1/`.

Để bật checkout webhook, cấu hình `SEPAY_WEBHOOK_API_KEY`, `SEPAY_ACCOUNT_NUMBER` và `SEPAY_BANK`. Trong SePay, chọn webhook tiền vào, xác thực API Key, tài khoản nhận đúng với cấu hình, prefix `SC` cùng hậu tố 16 ký tự chữ/số. Đặt endpoint thành `https://capillary-sled-monogamy.ngrok-free.dev/api/sepay/webhook/`. Trang checkout tạo mã QR theo cổng và đơn cụ thể. Webhook đối chiếu API key, tài khoản, chiều giao dịch, mã đơn, số tiền và ID giao dịch duy nhất. Đơn đã xác minh lưu trong DB trước khi subscriber thử phân cổng và gửi lệnh. Khi chưa xác định thiết bị đã nhận lệnh do timeout, backend giữ phiên để kiểm tra thủ công, tránh gửi lại lệnh cũ. Giao dịch tới sau hạn của đơn cần đối soát thủ công.

Cho OTA, giữ `FIRMWARE_HTTPS_BASE_URL=https://capillary-sled-monogamy.ngrok-free.dev/`, khai báo khóa riêng từng thiết bị trong `OTA_DEVICE_KEYS_JSON` và cấu hình firmware `trust_base` cùng URL. Các endpoint manifest/binary giữ kiểm tra capability và HTTPS. Đọc [giao thức OTA Secure](../firmware%201.0/OTA_MQTT_PROTOCOL.md) để chạy tunnel, upload artifact và triển khai. Webhook chỉ mở thêm đúng đường POST sau khi có API key. Dashboard và admin vẫn dùng địa chỉ nội bộ.

## Hợp đồng cần triển khai trước khi mở điều khiển trên broker công cộng

- **Sạc.** Web có serializer tham chiếu `signed_start_charging`, chưa nối vào đường gửi Firmware 1.0. Chủ đề vẫn là `iot/charge_station/v1/orders`. JSON chứa `device`, `command: "START_CHARGING"`, `port`, `session`, `amount_vnd`, `expires_at` (Unix UTC giây), `nonce` (32 ký tự hex thường ngẫu nhiên) và `signature`. Chuỗi HMAC-SHA256 dùng ASCII, số nguyên thập phân, dấu xuống dòng LF, không có LF cuối. Khóa sạc 32 byte riêng từng thiết bị, tách khỏi khóa OTA.
  ```text
  SC-CHARGE-1\n{device}\nSTART_CHARGING\n{port}\n{session}\n{amount_vnd}\n{expires_at}\n{nonce}
  ```
- **Firmware thực thi.** Từ chối gói retained, trường lạ/trùng, device hoặc port sai, HMAC sai, đồng hồ chưa đồng bộ, thời hạn đã qua hoặc vượt 30 giây, nonce đã nhận, relay/ngõ đo chưa sẵn sàng và trạm đang sạc/OTA. Xác minh chữ ký constant-time và ghi nonce đã chấp nhận vào NVS trước khi cấp điện. Giữ nonce tối thiểu tới hết hạn, kể cả khi reboot. Cùng nonce qua MQTT QoS 1 chỉ được thực thi một lần. Chế độ broker công cộng phải từ chối hoàn toàn lệnh văn bản cũ.
- **Hoàn tất sạc.** Firmware 1.0 chỉ gửi `device/port/status`. Cần `session` và nonce của lệnh đã chấp nhận, chữ ký HMAC cho báo cáo ACK/DONE trong miền chữ ký riêng, cùng kiểm tra chống replay. Backend chỉ được hoàn tất phiên đang khớp chính xác mã phiên và nonce đã lưu.
- **OTA.** Giữ canonical `SC-CMD-1` và `SC-MANIFEST-1` cho chế độ hiện có. Chế độ công cộng cần phiên bản giao thức mới hỗ trợ trạng thái OTA đã ký theo thiết bị, ràng buộc request ID, sequence OTA, version, status, progress và bộ đếm báo cáo chống replay. `SUCCESS` phải đến từ Main sau health check. Không đưa URL bearer tải firmware lên topic công cộng. Thiết bị phải lấy capability riêng qua HTTPS có xác thực thiết bị hoặc dùng một cơ chế phân phối capability tương đương đã thống nhất với firmware.
- **Điều kiện tích hợp.** Web công cộng hiện chỉ subscribe log và telemetry. Firmware 1.0 khóa relay/OTA trong bench mode, không triển khai parser lệnh sạc đã ký hay báo cáo có chữ ký. Chỉ mở sau khi có kiểm thử firmware cho chữ ký sửa đổi, nonce lặp sau reboot, DONE/OTA SUCCESS giả và rollback. PR này không chứa thay đổi firmware.
