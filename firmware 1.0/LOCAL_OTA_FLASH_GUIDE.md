# Nạp USB lần đầu và OTA local qua ngrok

Áp dụng cho ESP32-WROOM-32, ESP-IDF **5.5.1**, flash 4 MiB. Mã nguồn được chỉnh
trong `firmware 1.0/`; Django chỉ được đọc để đối chiếu. Không chạy các lệnh flash
trong tài liệu này nếu trạm còn nối tải hoặc đang có phiên sạc.

## 1. Kiến trúc và điều kiện trước khi nạp

| Vùng | Offset | Dung lượng | Nội dung |
|---|---:|---:|---|
| factory | 0x10000 | 0x100000 | Recovery, không ADC/LVGL/payment |
| ota_0 | 0x110000 | 0x2F0000 | SmartCharge |

Bảng phân vùng không đổi. OTA chỉ ghi `ota_0`; không ghi Recovery, bootloader
hoặc partition table. Hai app là hai binary riêng. `initial_flash.py` là công cụ
UART cho lần chuyển layout, không được gọi tự động trong build hoặc OTA.

**Trước khi nạp USB:** ngắt tải vật lý; kết thúc phiên sạc; xác nhận relay OFF;
kiểm tra GPIO25 active-high và pulldown ngoài. Phần mềm kéo LOW ở đầu hai app
nhưng không bảo đảm mức chân trong ROM/bootloader khi reset. Không bỏ qua việc
ngắt tải vì chỉ nhìn thấy LED relay OFF.

Máy phải có ESP-IDF 5.5.1 và một broker MQTT TLS riêng đang hoạt động. Cần biết
host/port, tài khoản riêng ESP32, tài khoản backend và cùng HMAC key 32 byte.
Không dùng `broker.hivemq.com`, kể cả hostname viết hoa hoặc có dấu chấm cuối.

## 2. Build hai ứng dụng

Tại thư mục `firmware 1.0` trong PowerShell mới:

```powershell
. "C:\Espressif\tools\Microsoft.v5.5.1.PowerShell_profile.ps1"
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -D PROJECT_VER=1.0.0 build
Push-Location recovery
idf.py -B build-recovery -D SDKCONFIG=sdkconfig.recovery build
Pop-Location
python tools/verify_builds.py
python -m unittest discover -s tests -v
```

Dùng sdkconfig mới riêng, không tái sử dụng sdkconfig 2 MB cũ. Hai binary phải
qua strict guard: Recovery <1 MiB; SmartCharge <0x2F0000. Verifier kiểm tra
hai partition binary giống nhau, rollback bật, flash 4 MB, chip ESP32, địa chỉ
nạp và Recovery không liên kết ADC/LVGL/payment. Không sửa bootloader IDF.

Tests HTTP chạy mã C production bằng native compiler: Windows nhận `CC` hoặc
`build-host/tcc/tcc.exe`; Linux dùng `cc`. Không có native compiler thì nhóm test
này SKIP, không được báo PASS. Xtensa cross-compiler không chạy được DLL host.
Test đối chiếu Web đọc source Django cạnh thư mục này, không import Django,
không truy cập DB, không publish MQTT và không sửa file Web.

## 3. Provision Wi-Fi, broker và trust_base

```powershell
New-Item -ItemType Directory -Force provisioning | Out-Null
Copy-Item provision.ngrok.example.json provisioning/device.json
```

Sửa `provisioning/device.json` trên máy, không commit secrets:

- `device`: `ESP32_001`.
- `ssid` / `password`: Wi-Fi thật.
- `broker`: URI `mqtts://` của broker riêng, thường port 8883. Không nhập URI
  chứa userinfo, query, fragment hoặc HTTP path.
- `mqtt_user` / `mqtt_password`: tài khoản thiết bị có quyền subscribe orders và
  OTA command, publish telemetry/status/log. Backend dùng account riêng có ACL
  publish command và subscribe output; không bắt buộc cùng username/password.
- `trust_base`: **`https://capillary-sled-monogamy.ngrok-free.dev/`**, có `/` cuối.
- `key_hex`: có thể bỏ để tool tạo key ngẫu nhiên. Nếu đã có key của thiết bị trên
  Django, nhập đúng 64 ký tự hex để không tạo key mới ngoài ý muốn.

Domain chỉ nằm trong dữ liệu provisioning/tài liệu, không hard-code trong C.
Không thay bằng URL local `http://127.0.0.1:8000`: ESP32 tải qua HTTPS ngrok.

```powershell
python tools/provision.py --config provisioning/device.json --out provisioning/ESP32_001 --bootstrap
```

Tool tạo NVS 16 KiB, CSV và `device-secret.json`. Key được chuẩn hóa lowercase
để phù hợp validator `OTA_DEVICE_KEYS_JSON` của Django. Giữ file secret ngoài
Git/HTTP, không dán key/password lên chat hoặc screenshot.

## 4. Đồng bộ cấu hình với phía Web

AI/người vận hành Web phải cấu hình bên Web; các lệnh ở đây chỉ là hướng dẫn,
không được firmware tool tự chạy hoặc tự sửa Django:

```text
FIRMWARE_HTTPS_BASE_URL=https://capillary-sled-monogamy.ngrok-free.dev
OTA_DEVICE_KEYS_JSON={"ESP32_001":"<cùng key_hex lowercase trong NVS>"}
MQTT_BROKER=<cùng host của NVS, bỏ mqtts:// và bỏ port>
MQTT_PORT=8883
MQTT_USERNAME=<backend account>
MQTT_PASSWORD=<backend account password>
OTA_STATUS_TIMEOUT_SECONDS=900
OTA_DOWNLOAD_TTL_SECONDS=7200
```

Secret mapping trên là cấu trúc minh họa, không phải key thật. Backend và NVS
phải dùng **cùng 32 byte**, đúng device ID. Device ID MQTT client của ESP32 là
`ESP32_001`, backend cần client ID khác. Xác nhận CA của broker thuộc bundle
ESP-IDF; không bỏ certificate/hostname verification để xử lý lỗi chứng chỉ.

Phía Web chạy Django ở port thực tế và ngrok trỏ đúng port, ví dụ port 8000:

```powershell
# Do người vận hành Web chạy tại môi trường của Web:
python manage.py runserver 0.0.0.0:8000
ngrok http 8000 --url=https://capillary-sled-monogamy.ngrok-free.dev
# Một terminal riêng chạy subscriber, không chạy trong mỗi WSGI worker:
python mqtt_subscriber.py
```

ALLOWED_HOSTS/CSRF/trusted origin phải chứa domain thật theo cấu hình Web.
Ngrok là đường HTTPS cho manifest/bin; MQTT vẫn đi trực tiếp broker TLS riêng.

Kiểm tra read-only từ máy trước khi nạp:

```powershell
python tools/probe_https.py --base https://capillary-sled-monogamy.ngrok-free.dev
```

Tool kiểm tra root và một manifest URL cố tình không có quyền; **không tạo OTAJob,
không publish lệnh, không in token/URL capability, không ghi flash**. 403/404 ở
manifest giả có thể đúng; `ERR_NGROK_3200` nghĩa endpoint ngrok offline, phải sửa
tunnel trước. PC TLS pass không chứng minh TLS trên ESP32; còn phải thử trên board.

Nếu có một command JSON hợp lệ do Web cung cấp, có thể kiểm tra header của URL
manifest đó qua `--command-file provisioning/command.json`. Đây chỉ là probe
header/prefix, không thay cho kiểm tra HMAC/SHA/image trên thiết bị.

Firmware gửi `ngrok-skip-browser-warning: 1`, `Accept-Encoding: identity` và
User-Agent không phải browser. Header này được ngrok hỗ trợ phía client;
không phải tắt xác thực hoặc bỏ TLS. Tham khảo [ngrok Free plan](https://ngrok.com/docs/pricing-limits/free-plan-limits).

## 5. Backup và nạp USB có kiểm soát

Sau khi đã **ngắt tải và xác nhận relay OFF**, thay COM3 bằng port thật:

```powershell
python tools/initial_flash.py --port COM3 --nvs provisioning/ESP32_001/nvs.bin --backup provisioning/flash-before-migration.bin
```

Installer kiểm tra artifacts, xác nhận flash vật lý 4 MB bằng esptool, backup
đủ 4 MiB rồi mới ghi. Backup path phải chưa tồn tại. Backup có thể chứa secrets;
không upload lên Git. Nếu chuyển từ layout NVS 24 KiB cũ sang 16 KiB mới, không
giữ nguyên các sector cũ: phải provision lại. Không flash lại NVS ở mỗi OTA vì
sẽ reset seq/replay floor và metadata. Không ghi eFuse qua công cụ này.

`--bootstrap` cho phép Recovery kiểm tra và chọn main được nạp vật lý qua UART
một lần. Nó không cho phép boot một binary download chưa được xác minh.
Mất nguồn trong initial UART migration có thể cần nạp lại UART; cơ chế chịu
mất điện OTA chỉ áp dụng sau khi layout/Recovery đã được cài đúng.

## 6. UART boot và kiểm tra chức năng cũ

```powershell
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -p COM3 monitor
```

Kiểm tra theo thứ tự:

1. Recovery chạy từ factory, relay OFF; bootstrap chọn main `ota_0`.
2. Flash physical=4 MiB/configured=4MB, partition offsets đúng.
3. ADC GPIO34/GPIO35, 20 kHz; WCS1800/ZMPT101B; ILI9341 và layout cũ hiển thị.
4. Wi-Fi, SNTP, MQTT TLS connect. Candidate chỉ health-confirm sau >=30 giây
   ổn định, mẫu đo hợp lệ, LVGL heartbeat và MQTT sẵn sàng; timeout startup 90 giây.
5. MEM: free/minimum/largest internal heap và stack watermark main/network/
   telemetry/LVGL. Đo tiếp trong lúc Recovery có cả MQTT TLS và HTTPS, không chỉ
   nhìn linker/static DRAM hoặc số heap của firmware cũ.
6. MQTT telemetry khoảng 10 giây, đủ V/I/P/Wh, device/port/charging/status/interval.
   Không có raw ADC hoặc log mỗi giây. IDLE giữ total phiên vừa kết thúc; delta
   bằng 0 ở khoảng hoàn toàn idle. DONE vẫn qua topic status cũ.

Trước khi nối lại tải, thực hiện test relay/phiên sạc với tải kiểm thử phù hợp.
Không tự bật lại phiên cũ sau reset.

**Không thay đo lường để thử OTA:** bản này giữ nguyên byte của power_meter.c,
display_lvgl.c và screen layout so với Firmware 1.0 đã đăng Git. Firmware cũ trước
1.0 dùng P=Vrms*Irms và main nhân PF=0.9; Firmware 1.0 đã dùng covariance mẫu I/V
và tích phân Wh theo monotonic dt. Đây là khác biệt có sẵn, không được hiệu chuẩn
lại trong lần tích hợp này. Chưa chứng nhận P/Wh tương đương firmware cũ; cần một
thay đổi/test đo lường riêng nếu muốn đổi thuật toán. Đơn giá vẫn 0.9 Wh/10,000 VND,
không đổi thành 130 Wh theo comment cũ chưa được xác nhận.

## 7. Test OTA 1.0.0 -> 1.1.0

Sau khi board đã chạy main 1.0.0 và telemetry/health ổn định:

```powershell
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -D PROJECT_VER=1.1.0 build
python tools/verify_builds.py
```

Upload **chỉ** `build-smartcharge/smartcharge.bin` loại main trên Django Admin,
duyệt release rồi chọn device ESP32_001 đang rảnh. Version descriptor phải
`1.1.0` (không dùng literal `1.1`). Không upload merged image/Recovery/bootloader.

Web tạo command có seq lớn hơn NVS, request ID mới, URL capability, HMAC đúng.
Firmware kiểm tra replay trước busy để QoS1 duplicate không hủy job đã nhận.
Đang sạc phải REJECTED_BUSY, không tắt relay hoặc reboot.

Job đúng: ACCEPTED -> REBOOTING -> RECOVERING -> DOWNLOADING -> VERIFYING ->
INSTALLING -> REBOOTING -> SUCCESS. SUCCESS chỉ do main mới phát sau health
check, đúng version; PUBACK/100% download không thay thế SUCCESS.

Không dùng signer offline để giả thành OTAJob Web: backend cần job và capability
thật để chấp nhận URL/status. `tools/ota_protocol.py` chỉ là reference/test.

## 8. Retry, mất nguồn và các giới hạn cần phía Web phối hợp

Trước `esp_ota_begin(ota_0)`, Recovery phải chọn factory và kiểm tra lại boot
target. Trong lúc ghi, otadata giữ factory; chỉ chọn main sau SHA và image verify.
Mất nguồn/mạng khi pending hợp lệ sẽ tải lại từ byte 0 khi có nguồn/mạng, không
cần Admin gửi command mới **trong khi capability phía server vẫn được chấp nhận**.

Transient TLS/network/timeout/interrupted download/HTML ngrok: giữ pending,
report RECOVERING kèm `error`, retry 5/10/20... tối đa 300 giây. Header/status lỗi
được ghi bằng mã cố định; không in URL, token, server body hay credentials.
401/403 capability hết hạn, redirect, sai MIME/length, manifest MAC/hash/image sai
là lỗi cứng: giữ Recovery OFF, lưu reason, báo FAILED và chờ request mới hợp lệ.
Không tự sửa URL/token/seq/HMAC hoặc tắt TLS để vượt lỗi server.

**Đối chiếu Web mới nhất, không sửa Django trong nhiệm vụ này:**

- Topic, seq-string, HMAC canonical, request/device/version/SHA/manifest schema
  khớp. Test đọc các biểu thức tạo command/manifest thật trong Web đã qua.
- `myapp/ota_services.py::process_ota_status()` hiện đã cho RECOVERING/progress
  restart, nhưng còn tự đổi FAILED sau bước Recovery thành RECOVERING để hỗ trợ
  firmware cũ. AI Web cần nhận biết lỗi terminal của firmware mới qua `error`
  (ví dụ ESP_ERR_INVALID_CRC, HTTPS_AUTH_OR_CAPABILITY_EXPIRED,
  OTA_BOOT_VALIDATION_FAILED), thay vì giữ job active cho đến hết TTL. Không đổi
  tên topic hoặc canonical để sửa vấn đề này.
- Cùng hàm trả sớm với status/progress giống hệt nhau mà không refresh
  last_status_at. Heartbeat RECOVERING giống nhau có thể không giữ job sống.
  AI Web cần refresh thời gian trên duplicate đã xác thực/đúng device/request.
- `authorize_job_token()` và timeout worker vẫn hết hạn/đóng job theo TTL/timeout.
  Nếu mất điện lâu hơn giới hạn đó, pending ở NVS không làm capability hợp lệ
  trở lại. Cần Web gia hạn/khôi phục capability cho job hoặc phát command mới;
  firmware không thể tự tạo quyền truy cập mới. Tránh hứa tự phục hồi vô hạn.
- `smartcharge/OTA_MQTT_PROTOCOL.md` vẫn nói progress không đi lùi dù code Web đã
  cho restart. AI Web cần cập nhật tài liệu cho khớp behavior mới.

Đây là báo cáo phối hợp; không tự thay hợp đồng MQTT hoặc file Django.

## 9. Power-cut test trên board thử nghiệm

Giữ tải ngắt và một backup flash tốt. Không dùng board đang phục vụ thanh toán.

| Điểm fault | Kết quả cần quan sát, chưa được tự coi PASS |
|---|---|
| Tải 10%, 50%, 90%, rút nguồn | Boot factory, relay OFF, pending còn, tải lại từ byte 0 |
| Trước boot commit | Tiếp tục Recovery; không chọn image ghi dở |
| Sau boot commit, trước health confirm | Candidate hợp lệ hoặc rollback Factory ở lần boot sau |
| Mất Wi-Fi giữa tải | Không boot main ghi dở; retry cùng request khi mạng trở lại |
| 200 HTML/redirect/hash sai | Không kích hoạt main; HTML/prefix lỗi bị chặn trước begin/write |
| Candidate crash/reset trước health | Bootloader pending -> aborted -> Recovery |
| Candidate khỏe | Main xác nhận version mới và SUCCESS sau health |

Lặp mỗi điểm nguồn ít nhất 10 lần, lưu boot log/partition/state thực tế. Kiểm tra
otadata bằng công cụ IDF trên board thử nghiệm; không xóa factory/bootloader.
Có thể nhấn reset trước mốc healthy để kiểm tra nhánh pending reboot, nhưng test
đó không thay cho thử watchdog/crash thật hoặc brownout. App đã VALID không tự
rollback chỉ vì watchdog; xem giới hạn boot counter trong OTA_MQTT_PROTOCOL.md.

Host model tests và native HTTP guard tests không phải power-cut PASS. Phiên
kiểm tra này không tự chạy initial_flash.py hoặc điều khiển relay/phần cứng.

## 10. Bảng xử lý lỗi

| Lỗi | Hành động |
|---|---|
| ERR_NGROK_3200 | Khởi động/kiểm tra lại ngrok agent và domain/port; không đổi TLS |
| HTTPS_HTML_OR_NGROK_WARNING | Kiểm tra tunnel/backend headers; header bypass đã có trong firmware |
| HTTPS_REDIRECT_REJECTED | URL capability phải đúng slash/path cuối; backend trả 200 trực tiếp |
| HTTPS_CONTENT_LENGTH_INVALID | Trả Content-Length đúng, không chunked hoặc transform/gzip body |
| HTTPS_CONTENT_TYPE_INVALID | Manifest application/json; binary application/octet-stream |
| HTTPS_AUTH_OR_CAPABILITY_EXPIRED | Kiểm tra job/TTL, khôi phục quyền phía Web hoặc cấp job mới |
| HTTPS_TLS_OR_CONNECT_FAILED | Kiểm tra Wi-Fi/DNS/SNTP/CA/hostname; xem verify_flags trên UART |
| ESP_ERR_INVALID_CRC | Đối chiếu key lowercase, canonical, hash binary; không tự bỏ verify |
| Replay/version bị từ chối | Seq phải tăng, ID mới, version X.Y.Z lớn hơn bản đang chạy |
| Heap thấp/stack watermark thấp | Dừng triển khai, thu MEM log lúc TLS; không suy đoán từ static size |

Nguồn: [ngrok warning header](https://ngrok.com/docs/pricing-limits/free-plan-limits),
[ERR_NGROK_3200](https://ngrok.com/docs/errors/err_ngrok_3200).
