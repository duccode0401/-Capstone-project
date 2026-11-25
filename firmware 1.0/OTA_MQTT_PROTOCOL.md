# SmartCharge Recovery OTA, telemetry và event log

> Cập nhật tích hợp local/ngrok 2026-11-21: xem
> [LOCAL_OTA_FLASH_GUIDE.md](LOCAL_OTA_FLASH_GUIDE.md) trước khi provision/nạp.
> Domain cấu hình qua NVS; C không hard-code ngrok. Phần xử lý lỗi hiện phân biệt
> RECOVERING (retry lỗi mạng/HTML ngrok) và FAILED (lỗi cứng), kèm trường `error`
> đã được parser Web hỗ trợ. Những khác biệt còn lại ở state/TTL phía Web được
> liệt kê trong guide, không tự sửa Django hoặc thay canonical/topic.

## Trạng thái và phạm vi

Hai ứng dụng ESP32 / ESP-IDF **5.5.1** độc lập:

- `smartcharge/`: ADC continuous, LVGL/ILI9341, thanh toán, relay, telemetry.
- `recovery/`: relay OFF, Wi-Fi, MQTT event/status, HTTPS streaming OTA; không LVGL,
  không khởi tạo ADC, không logic sạc, không nhúng binary SmartCharge.
- `components/sc_core/`: cấu hình NVS, giao thức xác thực, network queues, downloader.

Không chạy HTTP server trên ESP32. Không sửa source bootloader ESP-IDF.
Không có firmware domain, tài khoản production hay khóa chung hard-code.
OTA_SECURE yêu cầu broker MQTT TLS riêng, tài khoản MQTT, khóa HMAC và HTTPS tin cậy.
Để xem log và telemetry qua EMQX khi đã ngắt tải, dùng
[chế độ test công cộng](PUBLIC_MQTT_TEST.md). Chế độ này khóa OTA và điều khiển tải.

Đã build và có kiểm tra artifact tự động; **chưa chứng nhận vận hành trên phần cứng**.
Xem `VALIDATION_RESULTS.md` để phân biệt kiểm tra đã chạy và checklist còn lại.

## Flash và initial migration

`partitions.csv` là nguồn chung duy nhất:

| Partition | Offset | Size | Vai trò |
|---|---:|---:|---|
| nvs | 0x9000 | 0x4000 | Cấu hình riêng, request, replay floor, trạng thái |
| otadata | 0xD000 | 0x2000 | Hai sector lựa chọn boot chuẩn IDF |
| phy_init | 0xF000 | 0x1000 | PHY |
| factory | 0x10000 | 0x100000 | Recovery, đúng 1 MiB |
| ota_0 | 0x110000 | 0x2F0000 | SmartCharge, 2.9375 MiB |

Cuối `ota_0` là `0x400000`, vừa đúng flash 4 MiB. App offset căn 64 KiB;
data offset/size căn 4 KiB, không overlap. Runtime kiểm tra flash vật lý và
offset/size; build kiểm tra bảng bằng công cụ ESP-IDF, subtype và strict size guard.
Recovery phải **nhỏ hơn** 1 MiB; SmartCharge phải **nhỏ hơn** 0x2F0000.
Linker kiểm tra static DRAM/IRAM; dynamic heap/stack bắt buộc đo trên board.

**Phải migrate bằng UART một lần. Không nạp SmartCharge vào 0x10000.**
Layout cũ có NVS 24 KiB, layout mới 16 KiB và dùng phần cuối NVS cũ làm otadata.
Không giữ nguyên NVS cũ bằng cách đổi mỗi partition table. Backup rồi provision lại.
Không dùng artifact/flash task `build-esp32` cũ cho layout này.

Trong PowerShell đã load ESP-IDF:

```powershell
. "C:\Espressif\tools\Microsoft.v5.5.1.PowerShell_profile.ps1"
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota build
Push-Location recovery
idf.py -B build-recovery -D SDKCONFIG=sdkconfig.recovery build
Pop-Location
python tools/verify_builds.py
```

Dùng sdkconfig mới riêng biệt để không vô tình tái sử dụng cấu hình 2 MB cũ.
`smartcharge-idf.ps1 -Action build` và `-Action recovery-build` thực hiện hai build này.
CMake sửa flash destination của SmartCharge thành `0x110000`; verifier kiểm tra lại
`flasher_args.json`. IDF có thể cảnh báo SmartCharge không vừa factory: đó là
artifact khác ứng dụng; guard subtype `ota_0` mới là điều kiện đúng.
Recovery có guard riêng `factory`, không được coi vừa `ota_0` là build hợp lệ.

Copy `provision.example.json` vào `provisioning/device.json`, nhập dữ liệu thật:
SSID, password, device ID, `mqtts://` broker riêng, user/password riêng thiết bị,
`trust_base` là HTTPS origin/path do bạn quản lý và **kết thúc bằng `/`**.
Không dùng domain ví dụ làm cấu hình production. `key_hex` có thể bỏ qua để tool
tạo 32 byte ngẫu nhiên riêng thiết bị. Giữ bản `device-secret.json` trong secrets
manager của backend, không đưa cho browser, không commit hoặc serve qua HTTP.

```powershell
python tools/provision.py --config provisioning/device.json --out provisioning/ESP32_001 --bootstrap
python tools/initial_flash.py --port COM3 --nvs provisioning/ESP32_001/nvs.bin --backup provisioning/flash-before-migration.bin
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -p COM3 monitor
```

`initial_flash.py` kiểm tra artifacts, dùng esptool xác nhận flash 4 MB, backup toàn
bộ flash rồi ghi hai app + bảng phân vùng + NVS mới + standard bootloader.
Lệnh này chỉ chạy khi người vận hành gọi trực tiếp; build không tự flash.
Thực hiện trên trạm rảnh, ngắt tải trước khi migrate. Backup chứa secrets.
Không ghi eFuse. Mất điện trong initial UART migration vẫn có thể cần nạp lại UART.

`--bootstrap` tạo state `bootstrap`: Recovery chỉ cho phép boot app được nạp vật lý
qua UART một lần, dùng `esp_ota_set_boot_partition()` để kiểm tra ESP image.
Không áp dụng ngoại lệ này cho download qua mạng. State được chuyển sang
`bootstrapped` trước khi chọn app; nếu mất điện đúng khoảng đó, Recovery chờ lệnh
OTA hợp lệ hoặc provision lại bootstrap qua UART. Không tự boot một app chưa rõ nguồn.
Không flash lại NVS khi cập nhật thông thường: sẽ reset credentials, replay floor
và lịch sử request. Chỉ thực hiện reprovision như một thao tác bảo trì có kiểm soát.

Request NVS V1 là blob `sc_request_t` 584 byte dùng chung header; compile-time
assert bảo vệ kích thước ABI. Không đổi thứ tự/ý nghĩa field ở main phiên bản
sau mà không giữ tương thích với Recovery đã nạp. Metadata/protocol migration
phải được thiết kế riêng nếu thay schema; Recovery không được thay bằng OTA này.

## Chứng minh lựa chọn boot từ source IDF 5.5.1

Đã đọc source tại `$IDF_PATH`:

1. `components/app_update/esp_ota_ops.c`, `esp_ota_set_boot_partition()`:
   kiểm tra image trước; khi target là factory, gọi `esp_partition_erase_range()`
   cho toàn bộ otadata. Khi target là OTA, cập nhật sequence/state lựa chọn OTA.
2. `components/bootloader_support/src/bootloader_utility.c`,
   `bootloader_utility_get_selected_boot_partition()`: hai record otadata rỗng
   chọn factory nếu có. Với rollback bật, `NEW` chuyển thành `PENDING_VERIFY`;
   lần boot sau nếu vẫn pending thì chuyển thành `ABORTED`. Không có OTA hợp lệ
   thì chọn factory.
3. Cùng file, `bootloader_utility_load_boot_image()`: nếu image OTA không load/
   verify được, thử phân vùng khác, bao gồm factory. Không đảm bảo recovery nếu
   chính Recovery, bootloader hay partition table cũng bị hỏng.

Đường ghi trong `sc_install()`:

```text
Đang chạy factory
 -> sc_go_recovery(): chọn factory, kiểm tra esp_ota_get_boot_partition()
 -> commit state pending
 -> lấy manifest đã xác thực
 -> esp_ota_begin(ota_0) / write streaming / SHA-256
 -> esp_ota_end(): ESP image + chip/revision/checksum
 -> commit state candidate
 -> esp_ota_set_boot_partition(ota_0)   [điểm commit boot duy nhất]
 -> reboot
```

Không ghi app cho đến khi chọn factory thành công. Nếu mất điện ở 10/50/90% tải,
otadata đã bị xóa và không thay đổi trong khi ghi app: bootloader chọn factory.
Recovery thấy state pending và tải lại **từ đầu**, retry 5, 10, 20... tối đa 300 giây.
Nếu mất điện trong lúc chuyển về factory trước khi erase hoàn tất, image main
vẫn chưa bị ghi: có thể boot main cũ hoặc Recovery; main thấy request pending
sẽ chuyển về Recovery trước khi nhận phiên sạc.
Nếu mất điện ở commit cuối, chỉ có app đã verify hoàn chỉnh được chọn; có thể
boot Recovery hoặc candidate, không chủ động chọn app đang ghi dở.

Đây là lập luận dựa trên thứ tự API và source, **không phải kết quả power-cut test**.
`tests/test_boot_model.py` chỉ là model của các trạng thái chọn boot, không thực
thi bootloader thật, không chứng minh flash-sector atomicity hay hành vi brownout.

## Boot validation, watchdog và giới hạn

Standard bootloader bật `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`, không bật eFuse
anti-rollback (không phù hợp factory layout này). `ota_0` mới đi vào PENDING_VERIFY.
Trong SmartCharge, relay OFF trước NVS/display/ADC/Wi-Fi; chưa nhận payment khi
startup chưa khỏe. Sau ít nhất 30 giây đo, >=5 mẫu tốt, LVGL heartbeat tốt và
Wi-Fi/MQTT sẵn sàng trên candidate, gọi `esp_ota_mark_app_valid_cancel_rollback()`.
Chỉ sau đó mới report SUCCESS kèm đúng version đang chạy.

Startup timer 90 giây reboot nếu chưa xác nhận. Main task được theo dõi bởi task
watchdog 20 giây; lỗi trước validation sẽ quay Recovery ở lần boot kế tiếp.
Recovery đếm candidate boot failure bền vững; sau 3 lần không tiếp tục tự cài lại
cùng request, giữ relay OFF và chờ request mới đã ký / UART. Mất điện trong khoảng
candidate commit cũng có thể được tính như boot failure, đây là cách fail-safe.

**App đã VALID không tự rollback chỉ vì watchdog.** NVS boot counter phát hiện
3 lần khởi động chưa đạt mốc healthy rồi chọn Recovery; những lần reset liên tiếp
có chủ đích cũng có thể kích hoạt cơ chế này. Sau validation, việc mất mạng đơn
thuần không khiến main đang ổn định phải reboot. Recovery có thể tải HTTPS dù
MQTT đang lỗi; trạng thái MQTT là best effort và không quyết định tính toàn vẹn.

Giới hạn: lỗi lặp xảy ra sau mỗi lần đạt healthy, lỗi rất sớm trước `app_main`,
hoặc một firmware hợp lệ nhưng cố ý không tuân thủ API health, không được cơ chế
counter/watchdog này bảo đảm phát hiện. Muốn bảo vệ các trường hợp đó cần boot
validation mạnh hơn / bootloader policy riêng / external supervisor. Không sửa
bootloader tiêu chuẩn khi chưa có yêu cầu phần cứng và threat model cụ thể.

GPIO25 active-high được kéo LOW và cấu hình pulldown ở đầu cả hai app. Phần mềm
không điều khiển được chân trong ROM/bootloader hoặc khi nguồn chưa ổn định:
cần điện trở pulldown ngoài và relay mặc định OFF để bảo đảm không có xung bật
trong toàn bộ quá trình reset. Chưa đo bằng oscilloscope nên chưa chứng nhận.

## Command và xác thực

Topics đều có prefix `iot/charge_station/v1/`:

| Suffix | Hướng | QoS | Nội dung |
|---|---|---:|---|
| orders | Web -> main | 1 | Giữ text payment `ESP32_001 1 10000` |
| ota/command | Web -> main/Recovery | 1 | JSON START_OTA đã ký, không retain |
| ota/status | thiết bị -> Web | 1 | Status + request/version/progress |
| status | main -> Web | 1 | Giữ JSON DONE cũ |
| telemetry | main -> Web | 0 | Snapshot mỗi 10 giây, best effort |
| logs | cả hai -> Web | 0 | Event log giới hạn |

Command bổ sung `seq` dạng **chuỗi số thập phân** và `signature`:

```json
{
  "device": "ESP32_001",
  "command": "START_OTA",
  "request_id": "ota-001",
  "version": "1.1.0",
  "manifest_url": "https://firmware.example.test/fw/manifest/ota-001",
  "seq": "1",
  "signature": "64-lowercase-hex-HMAC-SHA256"
}
```

URL/signature ở trên chỉ minh họa tài liệu, không nằm trong cấu hình production.
HMAC dùng khóa 32 byte riêng từng device, domain separation:

```text
SC-CMD-1\n{device}\nSTART_OTA\n{request_id}\n{version}\n{manifest_url}\n{seq}
```

Không có newline cuối. Canonical UTF-8, `seq` không có leading zero. Dùng
`tools/ota_protocol.py` làm reference phía Django, tránh tự ghép JSON để ký.
So sánh MAC constant-time. Sai device, signature, URL, version hoặc thiếu khóa
thì không chấp nhận. Retained messages bị bỏ. MQTT fragments được ráp vào buffer
giới hạn, đúng topic, offset và tổng size; callback chỉ đẩy command vào queue.

Sau xác thực, main kiểm tra idle, relay OFF, startup healthy; đang sạc trả
REJECTED_BUSY, không thay đổi phiên sạc. Main là chủ sở hữu duy nhất state relay/
payment/OTA, không dùng shared MQTT text buffer cũ.
Chấp nhận cần version X.Y.Z lớn hơn bản hiện tại, `seq` lớn hơn floor đã lưu NVS.
Floor được commit trước request, rồi commit pending trước reboot. Nếu mất điện
trong acceptance có thể mất lượt sequence; backend phải phát sequence mới.
Trùng request hiện tại bị từ chối. Backend phải giữ unique(device, request_id)
và sequence tăng đơn điệu, không ký lại một ID cũ với sequence mới. Replay một
message đã chấp nhận bị chặn ngay cả sau reset. REJECTED_BUSY chưa consume seq;
backend có thể chủ động gửi lại khi trạm rảnh.

Không mặc định dùng public broker cho OTA. Broker production phải xác thực và
ACL theo device: chỉ backend được publish command, thiết bị chỉ subscribe;
thiết bị chỉ publish status/telemetry/log phù hợp. Với topic chung, backend cần
lọc quyền subscribe hoặc chuyển sang topic theo-device nếu cần cô lập dữ liệu.
Payment text cũ **chưa có chữ ký/chống replay**: giữ để tương thích, production
phải dùng broker ACL; OTA authentication không tự bảo vệ payment cũ.

## Manifest và streaming HTTPS

Manifest trả HTTP 200, Content-Length chính xác, <=2048 byte, không redirect:

```json
{
  "device": "ESP32_001", "request_id": "ota-001", "version": "1.1.0",
  "url": "https://firmware.example.test/fw/releases/1.1.0.bin",
  "size": 1430000, "sha256": "64-lowercase-hex",
  "chip": "esp32", "seq": "1", "signature": "64-lowercase-hex-HMAC"
}
```

Canonical MAC:

```text
SC-MANIFEST-1\n{device}\n{request_id}\n{version}\n{url}\n{size}\n{sha256}\nesp32\n{seq}
```

Đây là **manifest được xác thực bằng HMAC**, không phải chữ ký bất đối xứng trên
binary. Hash, size, target, version, URL và request cùng được MAC bảo vệ bằng
khóa riêng đã provision; không chỉ tin SHA gửi cạnh file. TLS dùng CA bundle và
hostname verification, không tắt verify. Recovery dùng common-root CA bundle để
giữ dưới 1 MiB; endpoint phải có chain phù hợp. Đồng hồ được đồng bộ SNTP trước
TLS; SNTP không phải nguồn ủy quyền OTA. Khóa bị lộ trong NVS hoặc server bị chiếm
sẽ phá mô hình xác thực. Chưa bật Secure Boot/Flash Encryption/NVS encryption;
không chống được kẻ có quyền đọc/ghi flash vật lý. Cần thiết kế provisioning/eFuse
riêng trước sản phẩm production, không tự bật các eFuse không đảo ngược.

URL phải nằm dưới `trust_base` chính xác, HTTPS, không userinfo, query, fragment,
escape/traversal; redirect và chunked encoding không được hỗ trợ ở bản này.
Không dùng URL chứa credentials. Firmware endpoint phải trả Content-Length,
không gzip/transform body. HTTP timeout 10 giây, download deadline 10 phút,
stream buffer 1024 byte, không giữ image trong RAM. SHA-256 tính trong lúc ghi.
Kiểm tra image header ESP32, app descriptor `project_name=smartcharge` và version;
`esp_ota_end()` kiểm tra đầy đủ ESP image/chip revision trước boot commit.
Không OTA bootloader, partition table hoặc factory.

Django Admin quản lý release/version/file; backend tính hash từ binary thật,
kiểm tra version, tạo manifest **riêng request/device**, ký bằng secret của device,
publish command. Không lấy file/hash/URL tùy ý từ browser rồi ký mà chưa kiểm tra.
Endpoint phải giữ release bất biến trong khi pending; retry cùng request cần
trả cùng release. Bản vá lỗi dùng request/seq/version mới.

```powershell
# Build bản tiếp theo với version mới trước khi publish release:
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -D PROJECT_VER=1.1.0 build
# Dùng domain thật; hai biến URL phải nằm dưới trust_base đã provision:
python tools/provision.py --config provisioning/ESP32_001/device-secret.json --out provisioning/release-1 `
  --image build-smartcharge/smartcharge.bin --request-id ota-001 --seq 1 `
  --manifest-url $manifestUrl --image-url $imageUrl
```

Chỉ upload binary và manifest; không upload `device-secret.json`, `nvs.csv`,
NVS image hoặc flash backup. Gửi `command.json` từ backend qua topic command,
QoS1, retain=false. `tools/ota_protocol.py` có thể được import trong Django.

## Status, telemetry và logging

OTA status có `device`, `request_id`, `version`, `status`, `progress`.
ACCEPTED -> RECOVERING -> DOWNLOADING (bước 10%) -> VERIFYING -> INSTALLING ->
REBOOTING -> **SUCCESS chỉ sau boot health check trong main**. Có FAILED và
REJECTED_BUSY. Không xem ACK MQTT hoặc DOWNLOADING=100 là SUCCESS. SUCCESS được
main gửi lại mỗi 10 giây từ state bền vững để backend có thể nhận sau reconnect;
backend xử lý idempotent theo request_id. Progress/log có thể mất khi mạng lỗi.

Telemetry giữ schema người dùng yêu cầu, `interval_s` là thời gian thật giữa các
mốc telemetry (xấp xỉ 10 giây theo FreeRTOS scheduler). Task riêng lấy snapshot
qua critical section ngắn, không truy cập ADC hoặc LVGL, không gọi publish blocking.
Đo RMS liên tục khoảng 1 giây/cửa sổ; số đo telemetry là mẫu hoàn tất gần nhất,
không phải lấy giả một mẫu rồi nhân công suất cố định cho cả 10 giây.
Wh tích phân theo dt monotonic giữa các phép đo, trapezoid giữa công suất đo.
`energy_delta_wh` là chênh lệch accumulator đã tích phân giữa hai snapshot.
Khi không sạc, `charging=false`, `status=IDLE`, total giữ giá trị phiên vừa kết
thúc (0 sau boot), delta về 0 ở các khoảng hoàn toàn idle; payment mới reset total.
Delta có thể >0 trong snapshot IDLE đầu tiên nếu phiên kết thúc trong khoảng đó.
Một khoảng chứa hai phiên có delta là tổng điện năng của cả hai phần khoảng;
total luôn thuộc phiên mới nhất. Không lưu/khôi phục phiên sạc sau mất điện.

P được tính từ covariance mẫu I/V đã calibration, không nhân PF 0.9 cố định.
ADC multiplex có lệch thời điểm lấy mẫu và cảm biến có sai lệch pha/gain:
cần hiệu chuẩn thực tế trước dùng tính tiền; chưa chứng nhận độ chính xác metering.
**Đơn giá giữ nguyên code cũ: 10,000 VND -> 0.9 Wh.** Comment cũ nói 130 Wh không
khớp công thức, đã ghi rõ và bỏ comment gây hiểu nhầm. Chưa có xác nhận nghiệp vụ
nên không tự đổi giá. DONE vẫn gửi JSON theo topic status cũ, QoS1.

Event log chỉ gọi cho Wi-Fi/MQTT, payment, charge, relay, OTA, sensor, heap, reset;
không forward ESP_LOG/raw ADC/payload payment/URLs/passwords/token.
TX queue 8 message cố định, RX queue 3 command cố định, MQTT outbox tối đa 4096 B.
Main queue send timeout=0; worker dùng `esp_mqtt_client_enqueue()`.
Logs/telemetry có thể drop khi đầy/offline, đếm trong `dropped`; không có bộ nhớ
log vô hạn. DONE retry ở main cho đến khi enqueue; QoS1 có thể duplicate, backend
cần idempotent. Queue/outbox nằm trong RAM, reboot có thể mất event/DONE đang chờ;
không phải transaction ledger thanh toán bền vững.

Free/minimum/largest internal heap và stack high watermark (byte theo ESP-IDF)
của main, network, telemetry và LVGL được log định kỳ khoảng 10 giây. Recovery
log main sau mỗi lỗi và network mỗi 10 giây. Stack MQTT cấu hình 6144 B; chưa đo
high watermark nội bộ MQTT task trực tiếp. Linker pass không chứng minh peak heap
khi có hai kết nối TLS: cần xem MEM trong bài test tải HTTPS + MQTT.

## Checklist test trên board và server

Đường HTTP hiện yêu cầu manifest `application/json`, binary
`application/octet-stream`, Content-Length dương đúng giới hạn, HTTP 200,
không redirect/chunked/gzip. Header ngrok-skip-browser-warning=1 và User-Agent
SmartCharge-OTA/1 được gửi cho cả manifest lẫn binary. HTML ở header hoặc prefix
bị từ chối trước esp_ota_begin; không in URL capability/server body lên log.
Mã lỗi HTTPS_* đi qua UART và trường error trên OTA status. Sai MAC/hash/image
hoặc capability 401/403 giữ Recovery và chờ request mới, không retry vô hạn.
Lỗi transient vẫn giữ pending và tải lại từ đầu, với điều kiện server chưa thu
hồi/hết hạn capability. Firmware không có quyền tự gia hạn token phía Django.

1. Disconnect tải, flash migration, đo GPIO25 từ reset bằng oscilloscope. Relay
   phải OFF cả trong Recovery, startup, lỗi sensor, reboot. Không tự nối lại phiên cũ.
2. Capture MQTT telemetry ít nhất 2 phút, đo khoảng cách ~10 giây; so V/I/P với
   đồng hồ chuẩn; thử tải thay đổi, so tổng delta với Wh của phiên và thời gian thực.
3. Payment đúng format/device/port: bật relay, CHARGING telemetry, tắt theo target,
   DONE theo topic cũ. Sai device/port/busy không bật relay; kiểm tra UI, Wi-Fi reconnect.
4. Lệnh OTA sai device, sai HMAC, thiếu seq, replay sau reboot, trùng request,
   URL HTTP/khác host/redirect phải không ghi app. Command hợp lệ lúc sạc phải
   REJECTED_BUSY và không thay đổi phiên; không consume sequence cho busy.
5. Tạo release mới đúng version descriptor; thử hash sai, manifest MAC sai,
   size quá lớn, file bị cắt, wrong chip/revision/project, cert sai/expired và mất
   mạng. Recovery giữ relay OFF, không boot app ghi dở, retry có backoff.
6. Cắt nguồn vật lý ở DOWNLOADING 10%, 50%, 90%; lặp từng điểm ít nhất 10 lần.
   Boot log phải chỉ factory, đọc lại otadata bằng parttool/esptool, request pending
   còn và tải lại từ byte 0. Cắt thêm ở pending commit, candidate commit và boot commit.
7. Candidate crash/hang trước 30 giây, hoặc không có MQTT 90 giây: watchdog/timer
   reset, bootloader báo ABORTED, chạy Recovery. Sau 3 failed candidate giữ Recovery,
   chỉ request mới hợp lệ mới thử tiếp. Soi state NVS và boot log, không chỉ Web UI.
8. Dùng board test có backup, làm hỏng byte trong ota_0 để kiểm tra image verify
   fallback factory; không làm hỏng factory/partition table/bootloader.
9. Candidate khỏe: sau >=30 giây gửi SUCCESS từ main đúng version; reboot lần nữa
   vẫn chạy main, các chức năng đo/UI/payment/DONE hoạt động như trước.
10. Mất MQTT nhưng HTTPS còn: Recovery vẫn tải. Đo heap/stack trong toàn bộ TLS
    handshake/download/reconnect. Không stack overflow, không heap liên tục giảm.

Không đánh dấu các test trên là PASS nếu chỉ chạy Python host tests hoặc build.
