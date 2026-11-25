# Kết quả tích hợp Firmware 1.0 / Django local qua ngrok

Ngày kiểm tra: **2026-11-21**. Chỉ sửa source trong `firmware 1.0/`.
ESP-IDF v5.5.1, Xtensa GCC 14.2.0, esptool 4.12.0, Windows.
Không sửa Django, không tự flash board và không publish lệnh OTA thật.

## 1. Kết quả build/artifact

| Artifact | Binary thực tế | Partition | Dư |
|---|---:|---:|---:|
| SmartCharge 1.0.0 | 1,437,120 B | 3,080,192 B | 1,643,072 B |
| Recovery 1.0.0 | 913,200 B | 1,048,576 B | 135,376 B |

Cả hai build PASS. `tools/verify_builds.py` PASS:

- ESP32, configured flash 4MB, rollback bật, eFuse anti-rollback tắt.
- Partition binaries giống hệt nhau, offset/alignment/size đúng, cuối flash 0x400000.
- Recovery factory @0x10000, đúng 1 MiB; main ota_0 @0x110000, size 0x2F0000.
- Subtype checks và strict size guards PASS; Recovery vượt 1 MiB sẽ fail build.
- `esptool image_info`: checksum và validation hash hợp lệ cho cả hai image.
- Flash args main @0x110000, Recovery @0x10000, không ghi main đè factory.
- Recovery core biên dịch SC_RECOVERY=1; symbol audit không có ADC continuous,
  power_meter, LVGL init/render, payment hoặc tích phân phiên sạc.
- Không chỉnh source bootloader/partition table/SDK. Bảng partitions.csv giữ nguyên.
- Build chạy được trong đường dẫn `firmware 1.0` có khoảng trắng; size target dùng VERBATIM.

SHA-256 của artifacts trong lần kiểm tra này:

```text
smartcharge.bin f1e4743f30ec31d7bd8b99a35dbaa18d51bdf2ddac6efef742e8c717d0c8577e
recovery.bin   ca3fb6fa23663c5310dd87c34642ac11d2a8ecccf7841f1511a590937b90bfce
```

Rebuild có thể đổi hash vì metadata/timestamp. Server phải tính hash từ binary
phát hành thực tế; không dùng hash trong tài liệu cho một bản build khác.

Warning còn lại: API LVGL cũ bị deprecated trong screen layout được giữ nguyên;
IDF default app check cảnh báo main không vừa factory, nhưng explicit check ota_0
và strict guard đều PASS. Không đổi layout/API chỉ để xử lý warning trong task OTA.

## 2. Tests đã chạy

```powershell
python tools/verify_builds.py
python -m unittest discover -s tests -v
```

**21 tests PASS, không SKIP** trong ESP-IDF Python environment sau build:

| Nhóm | Số test | Phạm vi thật sự |
|---|---:|---|
| Boot model | 4 | Mô hình factory/new/pending/valid; không chạy bootloader thật |
| HTTP policy C | 6 | Biên dịch và chạy C production bằng native TinyCC 0.9.27 |
| Protocol/reference | 7 | HMAC binding, schema, URL/chip/project và strict size guard |
| Provision/signer | 1 | NVS generator thật 16 KiB và signer đọc binary main thật, TEST key |
| Web contract | 3 | Đọc/đánh giá AST canonical và manifest thật của Web; không DB/import Django |

HTTP C tests kiểm tra HTTP 200 hợp lệ, redirect/auth/404/429/5xx, MIME sai,
HTML ngrok ngay cả HTTP 200, Content-Length âm/0/quá giới hạn, chunked/gzip,
HTML giả MIME binary, prefix image và manifest. Đây là unit tests của guard C;
không giả là test esp_http_client/TLS/flash trên ESP32.

Native compiler portable được đặt trong `build-host/tcc/` (generated/ignored),
không cài system-wide, không dùng để build firmware ESP32. Không có native compiler
thì test C sẽ SKIP trên máy khác; chỉ báo đủ 21 PASS nếu log thật xác nhận không skip.

## 3. Probe HTTPS thực tế

```powershell
python tools/probe_https.py --base https://capillary-sled-monogamy.ngrok-free.dev
```

Kết quả cuối phiên kiểm tra:

- TLS certificate và hostname verification trên **máy PC**: PASS.
- GET root và một manifest URL cố tình không có quyền: **HTTP 404**.
- Body ngrok có **ERR_NGROK_3200**, Content-Type text/plain, Content-Length 83.
- Probe trả exit code 1: endpoint offline, không được coi là integration PASS.
- Chưa có manifest/bin được authorize thật để thử tải; không tạo OTAJob hoặc
  publish MQTT, không ghi flash. Domain có thể thay trạng thái sau thời điểm probe.

[Ngrok định nghĩa ERR_NGROK_3200 là endpoint offline](https://ngrok.com/docs/errors/err_ngrok_3200).
Người vận hành Web cần bật đúng tunnel/domain/port trước khi chạy E2E.
PC TLS PASS không chứng minh CA/heap/handshake trên ESP32; cần board test.

Firmware gửi `ngrok-skip-browser-warning: 1`, `Accept-Encoding: identity`,
Accept theo manifest/bin và User-Agent SmartCharge-OTA/1. Không tắt verify TLS
hoặc hostname. Header bypass ngrok là cơ chế client được
[ngrok hỗ trợ](https://ngrok.com/docs/pricing-limits/free-plan-limits), không thay thế auth.

## 4. Những gì đã sửa

| File | Thay đổi |
|---|---|
| components/sc_core/sc_update.c | Ngrok headers; status/MIME/encoding/length guards; prefix check trước erase/write; TLS/error diagnostics cố định; phân loại retry |
| components/sc_core/sc_http_policy.c + include/sc_http_policy.h | Guard HTTP portable, allocation-free, native C tests dùng đúng source này |
| components/sc_core/sc_core.c | Broker case-insensitive; URL authority/ASCII validation; pre-check replay; lưu reason lỗi terminal; giữ nguyên request ABI 584 B |
| components/sc_core/sc_network.c | Status hỗ trợ error đã có ở Web; topics/QoS/queues/payment contract không đổi |
| components/sc_core/include/sc_core.h + CMakeLists.txt | API nội bộ/helper mới và đăng ký C source, không đổi request struct/schema canonical |
| recovery/main/recovery_main.c | Transient -> giữ pending/RECOVERING/backoff; hard error -> reason bền vững/FAILED; không gọi main ghi dở |
| smartcharge/main.c | Check replay/version trước BUSY; QoS1 duplicate không gửi FAILED/BUSY làm hủy job đã nhận |
| tools/provision.py | Broker URI/hostname validation, chặn public broker case variants, chuẩn hóa key lowercase phù hợp Django |
| tools/ota_protocol.py | URL validation reference khớp runtime, không thay chuỗi HMAC |
| tools/probe_https.py | Probe TLS/ngrok read-only, không log URL capability/token/body |
| provision.ngrok.example.json | Device ESP32_001 + trust_base domain thật, không chứa broker credentials hoặc key |
| tests/test_http_policy.py | Native C guard tests |
| tests/test_web_contract.py | Đối chiếu trực tiếp AST Web, read-only |
| cmake/artifact_checks.cmake | VERBATIM cho đường dẫn chứa khoảng trắng |
| .gitignore | Bỏ generated host compiler/test outputs và log |
| OTA_MQTT_PROTOCOL.md | Bổ sung ngrok/error/retry và liên kết hướng dẫn mới |
| LOCAL_OTA_FLASH_GUIDE.md | Build, provision, đồng bộ key/broker, backup/USB, UART, telemetry, OTA và fault tests |
| VALIDATION_RESULTS.md | Báo cáo kết quả thật và giới hạn hiện tại |

Không hard-code domain trong C; không hard-code secrets; không đổi topic, device
routing, HMAC canonical, seq-string, request_id, version/SHA/manifest fields.
Trường error là optional field đã được parser Web đọc, không thêm topic/status mới.

## 5. Bảo vệ chức năng trạm sạc và đo lường

Đối chiếu byte với snapshot firmware 1.0 đã có trên Git:

- partitions.csv: identical.
- smartcharge/power_meter.c và power_meter.h: identical.
- smartcharge/display_lvgl.c và screens/charge_test_screen.c: identical.
- Các thân hàm handle_payment, update_energy và telemetry_task: identical.
- GPIO LCD, ADC34/35, sample rate 20 kHz, gains/calibration/noise floor,
  relay GPIO25 active-high và đơn giá không thay đổi trong patch này.

Khác biệt đã có từ firmware cũ sang 1.0: cũ dùng P=Vrms*Irms, main nhân PF=0.9
và Wh theo giả định vòng 1 giây; 1.0 dùng covariance mẫu I/V và monotonic dt.
Lần tích hợp này không sửa lại thuật toán hoặc tự hiệu chuẩn. Chưa chứng minh
P/Wh tương đương board/firmware cũ; nếu cần thay phải tách task/test đo lường riêng.
Đơn giá vẫn 0.9 Wh/10,000 VND, không đổi thành 130 Wh theo comment cũ.

Đây là bằng chứng không thay source liên quan, **không phải regression PASS trên board**.

## 6. RAM và FreeRTOS

IDF size và linker PASS:

| Ứng dụng | Static DRAM | IRAM |
|---|---:|---:|
| SmartCharge | 114,020 B | 111,439 B |
| Recovery | 36,296 B | 90,563 B |

Không được gọi số DRAM dư từ linker là free heap runtime. Chưa đo heap free/min/
largest block và stack watermark trên firmware mới, đặc biệt MQTT TLS + HTTPS.
Các log MEM và watermark main/network/telemetry/LVGL hiện có được giữ nguyên.
Không tăng buffer image vượt 1024 B; manifest tối đa 2048 B. Không chạy HTTP server,
không ADC/LVGL trong Recovery; queues/outbox vẫn giới hạn như trước.

## 7. Điểm phối hợp với AI Web, chưa tự sửa Django

Đã đọc lại source Web local mới nhất sau khi nó thay đổi trong phiên làm việc:

1. Canonical command hiện nằm ở prepare_ota và manifest ở build_manifest, đều
   khớp byte với firmware reference; seq vẫn JSON string, không newline cuối.
2. process_ota_status đã cho phép RECOVERING và reset tiến độ khi tải lại. Tuy
   nhiên nhánh recovery_failure còn đổi FAILED thành RECOVERING để tương thích
   firmware cũ. Bản mới chỉ gửi FAILED cho lỗi cứng/boot validation cạn retry và
   có error cụ thể; AI Web cần nhận biết terminal failure để đóng job đúng lúc.
3. Duplicate status/progress giống hệt return trước khi refresh last_status_at.
   Heartbeat RECOVERING giống nhau có thể không giữ timeout; cần sửa phía Web.
4. authorize_job_token/expire_stale_ota_jobs vẫn thu hồi theo terminal state/TTL/
   timeout. Nếu offline lâu hơn giới hạn, NVS pending không khôi phục được URL
   đã hết quyền. Web phải có renewal/reissue hoặc operator cấp job mới. Firmware
   không tự tạo token/HMAC URL hoặc vượt quyền server.
5. Tài liệu Web vẫn nói progress không được đi lùi dù code cho phép retry reset;
   AI Web cần đồng bộ tài liệu. Tên topic/canonical/schema không cần đổi.

Handoff chi tiết, vị trí hàm và cách xử lý lỗi có trong LOCAL_OTA_FLASH_GUIDE.md.
Không có file Django nào được sửa bởi các thao tác firmware trong nhiệm vụ này.

## 8. Những kiểm tra CHƯA THỰC HIỆN

| Kiểm tra | Trạng thái |
|---|---|
| ESP32 TLS với ngrok và broker riêng | CHƯA CHẠY: thiếu kết nối board/credentials và tunnel đang offline |
| Telemetry MQTT 10 giây/logs thực tế | CHƯA CHẠY |
| ADC/LVGL/payment/relay/DONE trên board | CHƯA CHẠY |
| HMAC sai device/seq/replay/busy trên C firmware đang chạy | CHƯA CHẠY; host tests không thay thế |
| Cắt nguồn 10/50/90%, trước/sau commit | CHƯA CHẠY; boot model không phải power-cut PASS |
| Mất Wi-Fi giữa tải, certificate/hash/file lỗi trên board | CHƯA CHẠY |
| Main crash/watchdog trước health -> Recovery | CHƯA CHẠY |
| Main mới boot khỏe và report SUCCESS đúng version | CHƯA CHẠY |
| GPIO25 không có xung bật trong ROM/reset | CHƯA ĐO; cần pulldown ngoài và ngắt tải |
| Heap/stack dưới tải TLS và hiệu chuẩn P/Wh | CHƯA ĐO |

Trước flash: ngắt tải vật lý, chắc chắn relay OFF, backup flash; làm theo
LOCAL_OTA_FLASH_GUIDE.md. Không bật phiên sạc cũ sau reset. App đã VALID không tự
rollback chỉ vì watchdog; boot counter có giới hạn được mô tả trong protocol.
Firmware dùng HMAC manifest + verified HTTPS, chưa có secure boot/NVS encryption
để chống kẻ đọc/ghi flash vật lý. Không tuyên bố production-ready/E2E PASS.
