# Cập nhật SmartCharge từ xa qua OTA

Thiết bị phải có application và Recovery tương thích, phân vùng OTA, MQTT TLS riêng, khóa HMAC và HTTPS trust base đã provision. Public Test hiển thị **OTA DISABLED - SECURE PROVISIONING REQUIRED**. Nếu thiết bị chưa hỗ trợ Secure OTA, làm theo [hướng dẫn provision và nạp lần đầu](../firmware%201.0/LOCAL_OTA_FLASH_GUIDE.md). Sau khi Secure OTA hoạt động, cập nhật application qua Web.

## 1. Build và kiểm tra application

Mở ESP-IDF PowerShell trong thư mục Web `smartcharge`. Ví dụ cập nhật từ `1.0.0` lên `1.1.0`.

```powershell
Push-Location '..\firmware 1.0'
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -D PROJECT_VER=1.1.0 build
$firmwareBin = (Resolve-Path '.\build-smartcharge\smartcharge.bin').Path
Get-Item -LiteralPath $firmwareBin | Select-Object Name,Length
Get-FileHash -Algorithm SHA256 -LiteralPath $firmwareBin
Pop-Location
```

Upload application `smartcharge.bin`. Web kiểm tra version trong app descriptor, ESP32 target, project `smartcharge` và giới hạn slot dưới `0x2F0000` bytes. Không chọn Recovery, bootloader, partition table hoặc file flash gộp.

## 2. Cấu hình Secure mode

Mở `.env` hiện có và chỉnh từng giá trị cần thiết. Thay hostname mẫu bằng cấu hình đã provision.

```dotenv
MQTT_PUBLIC_TEST_MODE=false
MQTT_DEVICE_ID=ESP32_001
MQTT_BROKER=mqtt.example.com
MQTT_PORT=8883
MQTT_USERNAME=
MQTT_PASSWORD=
FIRMWARE_HTTPS_BASE_URL=https://ota.example.com
OTA_DEVICE_TRUST_BASES_JSON={"ESP32_001":"https://ota.example.com/"}
OTA_DEVICE_KEYS_JSON={"ESP32_001":"THAY_BANG_64_KY_TU_HEX_THUONG"}
```

- `OTA_DEVICE_KEYS_JSON` dùng đúng khóa 32 byte đã provision cho thiết bị. Giữ file `.env`, cấu hình provisioning và capability URL trong môi trường riêng.
- Đối chiếu cấu hình provisioning với `OTA_DEVICE_TRUST_BASES_JSON`. Host và cổng của `FIRMWARE_HTTPS_BASE_URL` phải trùng `trust_base` trên ESP32. Cấu hình provisioning của firmware dùng dấu `/` cuối.
- ACL broker riêng cần cho phép backend gửi `iot/charge_station/v1/ota/command` và ACK, thiết bị nhận lệnh và gửi `iot/charge_station/v1/ota/status`. Giới hạn quyền publish theo vai trò và bảo vệ thông tin đăng nhập. OTA status được tin cậy qua broker TLS và ACL.
- HTTPS phải phục vụ trực tiếp manifest và binary, có chứng chỉ tin cậy trên ESP32, đúng Content-Type và Content-Length. Tránh redirect, nén và chunked. Web kiểm tra HTTPS và SHA-256 trước khi phát lệnh. Mở OTA Management trên Web nội bộ.
- Để thử heartbeat trên Public Test, giữ alias dùng cho MQTT và đặt `MQTT_PUBLIC_TEST_LOGICAL_DEVICE=ESP32_001` để hiển thị ID logic trên Web.

## 3. Cập nhật Web trên Windows

Chạy trong thư mục `smartcharge`. Các lệnh dùng `.env` và database hiện có.

```powershell
if (!(Test-Path '.\.venv\Scripts\python.exe')) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test
.\.venv\Scripts\python.exe manage.py runserver --insecure 127.0.0.1:8000
```

`--insecure` phục vụ CSS và JavaScript khi `DEBUG=false` trên server phát triển cục bộ. Trên server triển khai, phục vụ static bằng cấu hình của môi trường triển khai.

Mở PowerShell thứ hai trong cùng thư mục.

```powershell
.\.venv\Scripts\python.exe mqtt_subscriber.py
```

Nếu database Secure chưa có quản trị viên, chạy `manage.py createsuperuser` bằng Python trong `.venv`. Tài khoản thao tác cần quyền xem, thêm OTA job và deploy firmware. Tài khoản upload cần quyền thêm firmware.

## 4. Upload và Start OTA

1. Đăng nhập [Django Admin](http://127.0.0.1:8000/admin/), mở [OTA Management](http://127.0.0.1:8000/dashboard/ota/) và bấm **Upload application .bin**.
2. Nhập version trùng app descriptor, chọn **SmartCharge main**, chip **esp32**, file và release notes. Phê duyệt triển khai sau khi kiểm tra. Đối chiếu kích thước và SHA-256 với kết quả PowerShell.
3. Quay lại OTA Management. Chọn `ESP32_001` và bản phát hành, bấm **Kiểm tra tương thích**. Thiết bị phải online, có telemetry cho thấy đang rảnh, không có phiên sạc hoặc OTA active. Thiết bị dùng v2 cần heartbeat mới ở chế độ NORMAL, MQTT kết nối và relay OFF.
4. Bấm **Start OTA**. Job bắt đầu ở Pending. Theo dõi **Lịch sử OTA** và [Device Logs](http://127.0.0.1:8000/dashboard/logs/?category=OTA). Phần trăm được lấy từ bản tin thiết bị. Khi thấy Health Check, Web đã nhận heartbeat của application mới và đang chờ xác nhận SUCCESS đúng request và version đích.
5. Khi nhận SUCCESS, đối chiếu version ở [Device Dashboard](http://127.0.0.1:8000/dashboard/devices/), OTA history và serial log ESP32 nếu có. Device Logs ghi riêng kết quả xử lý, publish ACK và PUBACK.

## 5. Kiểm tra lỗi và phục hồi

- **Pending lâu** — kiểm tra subscriber, broker TLS, ACL, provisioning và lỗi HTTPS trong OTA history.
- **Recovery hoặc tải lại** — xem mã lỗi mạng, trust base, Content-Length, SHA-256 và hạn URL. Recovery có thể báo RECOVERING và tải lại.
- **Timeout hoặc rollback** — đối chiếu version và trạng thái boot qua heartbeat hoặc serial. Xác định nguyên nhân trước khi tạo job mới. Nếu thiết bị vẫn ở Recovery, làm theo hướng dẫn phục hồi của firmware đã provision.
