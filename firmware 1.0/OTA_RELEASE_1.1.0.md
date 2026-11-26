# Phát hành SmartCharge 1.1.0 qua OTA

## Trạng thái thiết bị đang thử

Board dùng NVS Public Test và alias `SC_TEST_…`. Chế độ này không subscribe lệnh OTA hoặc lệnh sạc. Recovery cũng kiểm tra cùng cờ trước khi nhận và tải OTA. Không có lệnh MQTT hay lệnh USB debug nào chuyển từ Public Test sang Secure. ACK công cộng chỉ dùng cho chẩn đoán. HEADLESS_TEST_MODE không thể xác nhận candidate production.

Nếu muốn nâng cấp từ board hiện tại, cần bootstrap Secure **một lần tại chỗ**. Tạo NVS mới từ `provision.example.json` với Device ID thật (`ESP32_001`, `ESP32_002`, …), Wi-Fi, broker TLS riêng và tài khoản thiết bị, HMAC riêng 32 byte và `trust_base` HTTPS. Đồng bộ cùng khóa với Django và lưu cấu hình trong kho bí mật cục bộ. Firmware hiện tại giữ `device` làm alias trên wire trong Public Test, và có trường `logical_device` để xem ID trạm. NVS Public Test cũ vẫn đọc được, mặc định ID logic là `ESP32_001`.

Nếu slot Main hiện là bản **headless**, đổi NVS thôi chưa đủ. Cần chọn hoặc nạp Main **thường** có version thấp hơn 1.1.0 trong lần bootstrap này. Nếu board đã có Main thường phù hợp, chỉ cần provisioning Secure và chọn lại Main theo quy trình đã xác minh. Giữ Recovery cũ nếu đã xác nhận dùng cùng partition table và hỗ trợ giao thức Secure. Thực hiện backup, giữ tải ngắt, rồi dùng quy trình USB trong [hướng dẫn build](FIRMWARE_BUILD_GUIDE.md) nếu cần cài baseline. Dùng một thư mục build khác cho baseline để giữ nguyên file phát hành 1.1.0. Sau khi baseline Secure boot khỏe, kiểm thử 1.1.0 bằng Web OTA.

Chuẩn bị NVS Secure trong một thư mục mới, giữ file Public Test hiện có:

~~~powershell
New-Item -ItemType Directory -Force provisioning | Out-Null
if (-not (Test-Path provisioning/secure-esp32-001.json)) {
    Copy-Item provision.example.json provisioning/secure-esp32-001.json
}
notepad provisioning/secure-esp32-001.json
~~~

Sau khi lưu cấu hình và chọn thư mục đầu ra chưa tồn tại:

~~~powershell
python tools/provision.py --config provisioning/secure-esp32-001.json --out provisioning/secure-esp32-001 --bootstrap
~~~

## Build ảnh OTA trên Windows

Trong ESP-IDF PowerShell 5.5.1:

~~~powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
Set-Location 'D:\-Capstone-project-main\firmware 1.0'
. .\load-esp-idf.ps1
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota menuconfig
~~~

Tắt **HEADLESS_TEST_MODE** trong **Component config → SmartCharge**, lưu rồi chạy:

~~~powershell
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -D PROJECT_VER=1.1.0 build
if ($LASTEXITCODE -ne 0) { throw 'SmartCharge build failed' }
python tools/verify_builds.py
if ($LASTEXITCODE -ne 0) { throw 'Artifact verification failed' }
New-Item -ItemType Directory -Force build-smartcharge/ota-release | Out-Null
Copy-Item build-smartcharge/smartcharge.bin build-smartcharge/ota-release/smartcharge-1.1.0-esp32.bin
Get-FileHash -Algorithm SHA256 build-smartcharge/ota-release/smartcharge-1.1.0-esp32.bin
~~~

`tools/verify_builds.py` đọc cả artifact Recovery đã build. Nếu chưa có artifact Recovery, build theo hướng dẫn ở trên để kiểm tra offline. Bước này không cập nhật Recovery trên thiết bị.

| Ảnh phát hành từ build đã kiểm tra | Giá trị |
|---|---|
| File | `build-smartcharge/ota-release/smartcharge-1.1.0-esp32.bin` |
| Project / chip / version | `smartcharge` / ESP32 / `1.1.0` |
| Kích thước | 1,464,064 bytes |
| SHA-256 | `44f0e01edd504855f97d36b673c868d1b192c4895bbcae9a5269ad531e56c236` |

SHA trong bảng áp dụng đúng artifact đã kiểm tra. Nếu tự build lại, dùng SHA của file mới build khi đối chiếu Django.

## Upload và chạy OTA trên Web

1. Trên Django Secure, dùng broker riêng TLS, xác thực MQTT và ACL cho từng thiết bị. Đặt `OTA_DEVICE_KEYS_JSON` trong cấu hình bí mật cục bộ. Dùng đúng `FIRMWARE_HTTPS_BASE_URL` khớp với trust base NVS và để subscriber hoạt động. Không đưa token tải lên EMQX công cộng.
2. Kiểm tra HTTPS/ngrok từ PowerShell bằng `python tools/probe_https.py --base https://capillary-sled-monogamy.ngrok-free.dev/`. Nếu có `ERR_NGROK_3200`, khôi phục tunnel trước khi tạo job.
3. Django Admin → **Firmware artifacts → Add**. Chọn file application ở trên, version `1.1.0`, loại Main, chip `esp32`. So SHA rồi đánh dấu **Approved**. Không upload merged binary, Recovery hay bootloader.
4. Chờ telemetry **IDLE** mới, không có phiên sạc active. Tạo **OTA job**, chọn đúng thiết bị và ảnh đã duyệt, xác nhận deploy. Giữ tải ngắt khi thử nghiệm.
5. Theo dõi **ACCEPTED → RECOVERING → DOWNLOADING → VERIFYING → INSTALLING → REBOOTING → SUCCESS**. Xem UART bằng `idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -p COM3 monitor --no-reset`. Sau khi Main mới boot, đối chiếu `App version: 1.1.0`, heartbeat và trạng thái job. `SUCCESS` chỉ có sau xác nhận health check của Main đúng version.

Recovery cũ tiếp tục dùng các mốc trạng thái có sẵn. Nếu build Recovery ở commit này để sử dụng về sau, UART có thêm mốc download 0%, 25%, 50%, 75%, 100%. Phần trăm download không phải xác nhận kiểm tra SHA hay boot thành công. Không cần ghi đè Recovery chỉ để có các mốc log này.

## Khi OTA thất bại

- Job còn pending: kiểm tra subscriber, Device ID, khóa HMAC, ACL, telemetry IDLE và version phải cao hơn. Đừng gửi cùng lệnh bằng tay khi job đang chạy.
- Download lỗi: kiểm tra DNS, SNTP, TLS, tunnel, capability chưa hết hạn, SHA và dung lượng. Recovery retry lỗi mạng hoặc ở lại factory nếu image lỗi. Chỉ tạo job mới sau khi job cũ đã kết thúc.
- Candidate boot lỗi hoặc mất nguồn trước khi xác nhận: kiểm tra log Recovery và rollback. Recovery ở `0x10000` vẫn là điểm vào phục hồi. Nếu cần phục hồi UART, dùng backup và quy trình có kiểm tra phân vùng.
- Với board trần headless, dùng Public Test để kiểm MQTT/ACK. Kiểm luồng OTA bằng host hoặc bằng board Secure có cảm biến/LCD thật và đã ngắt tải. Bản headless không thay thế health check phần cứng production.

## USB debug

NORMAL in tóm tắt mỗi lần gửi và ACK ứng dụng. DEBUG bật JSON và packet ID. ERROR giữ warning/error. Gửi nguyên dòng qua UART0 115200 như `LOG NORMAL`, `LOG DEBUG`, `LOG ERROR`, `STATUS` hoặc `HELP`. Lệnh chỉ tác động log. Giờ log dùng UTC+7 sau SNTP, trước đó dùng uptime. OTA status dùng PUBACK và trạng thái job hiện có của Django, không chờ Application ACK v2. Các bản tin khác dùng `message_type` khớp topic, Message ID dạng `xxxxxxxx-xxxxxxxx` và giữ nguyên ID khi retry.
