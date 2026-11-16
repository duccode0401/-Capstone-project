# Kết quả kiểm tra Recovery OTA / telemetry / logging

Ngày: 2026-11-16. Môi trường: Windows, ESP-IDF v5.5.1, esptool 4.12.0,
ESP32, compiler Xtensa GCC 14.2.0. Đây là kiểm tra source/build/host;
**không phải chứng nhận phần cứng hoặc bản production đã triển khai**.

## Artifacts đã build thành công

| Artifact | Binary thực tế | Partition | Dư |
|---|---:|---:|---:|
| `build-smartcharge/smartcharge.bin` | 1,436,416 B | 3,080,192 B | 1,643,776 B |
| `recovery/build-recovery/recovery.bin` | 910,192 B | 1,048,576 B | 138,384 B |

Đã kiểm tra bằng `check_sizes.py --subtype ota_0/factory`, strict size guard
và `esptool image_info`. Cả hai image có checksum và validation hash hợp lệ.
SHA-256 của file binary trong lần kiểm tra này:

```text
smartcharge.bin 547114a683e91a40bf1512b9ce39224e44fd87a6c996f07a73de35abd7362ed9
recovery.bin   4743e8a8377ed4ad040925db7a5ea36d711b833497b373a17d900cce3e45f5b1
```

Hash có thể đổi khi rebuild do timestamp/build metadata, không dùng các giá trị
ở tài liệu để xác thực một release khác; signer luôn tính từ binary phát hành.

`tools/verify_builds.py` PASS:

- Hai partition binary giống hệt nhau, đúng bảng yêu cầu và kết thúc ở 0x400000.
- Các offset căn đúng, không overlap/overflow, đúng ESP32 / cấu hình 4MB.
- Rollback bật, eFuse anti-rollback tắt ở cả hai cấu hình.
- Flash args SmartCharge -> 0x110000, Recovery -> 0x10000.
- Recovery compile core riêng với SC_RECOVERY=1.
- Symbol audit Recovery không có power_meter, ADC continuous start, LVGL init/
  render, payment handler hoặc tích phân phiên sạc.
- Source bootloader chuẩn không bị sửa.

IDF default size check có một warning dự kiến: SmartCharge không vừa factory.
Hai binary có vai trò khác nhau; explicit subtype check và strict guard đều PASS.
Recovery vượt factory sẽ làm build thất bại dù vẫn có thể vừa ota_0.

## RAM static và giới hạn phép kiểm tra

`idf.py size` và linker PASS:

| Ứng dụng | Static DRAM | IRAM |
|---|---:|---:|
| SmartCharge | 114,020 B | 111,439 B |
| Recovery | 36,296 B | 90,563 B |

Đây không phải free heap lúc chạy. Đã cấu hình main stack 8192 B, MQTT stack
6144 B, network/telemetry stack 4096 B, LVGL stack 8192 B; queues/outbox có giới
hạn. Recovery dùng TLS buffers 4096 B và streaming image buffer 1024 B.
**Chưa đo peak heap và stack watermark trên firmware mới**, nhất là TLS handshake
MQTT + HTTPS đồng thời trong Recovery. Log RAM cũ của người dùng không chứng minh
bản firmware mới còn đủ heap. Cần chạy MEM diagnostics trên board trước triển khai.

## Tests host đã chạy

```powershell
python -m unittest discover -s tests -v
```

**12 tests PASS** trong ESP-IDF Python environment:

- Server reference: command MAC, thay device/seq/version/request làm đổi MAC,
  manifest hash/size/version binding, domain separation, URL không tin cậy,
  wrong-chip/wrong-project image bị signer từ chối.
- Strict size guard từ chối binary bằng hoặc vượt giới hạn.
- Provisioning tool gọi NVS generator thật, tạo NVS 16 KiB bằng TEST credentials;
  signer đọc binary SmartCharge thật và tạo command/manifest MAC hợp lệ.
- Bốn **model tests** về lựa chọn factory/candidate/pending/valid.

Các test Python không chạy C parser/MQTT callbacks/bootloader thật; không được
gọi là end-to-end test lệnh sai device, chống replay bền vững hay power-cut test.
Mã nguồn IDF được đối chiếu cho hành vi otadata, nhưng model không chứng minh
tính nguyên tử của flash trên phần cứng.

## Chưa chạy: cần board, tải kiểm tra và server đã provision

| Trường hợp yêu cầu | Trạng thái |
|---|---|
| Telemetry MQTT mỗi 10 giây trên board | CHƯA CHẠY |
| ADC/LVGL/Wi-Fi/MQTT regression trên board | CHƯA CHẠY |
| Payment -> relay ON -> CHARGING -> relay OFF -> DONE | CHƯA CHẠY |
| Wrong device / duplicate / replay sau reset trên C firmware | CHƯA CHẠY |
| OTA khi đang sạc -> REJECTED_BUSY, giữ phiên sạc | CHƯA CHẠY |
| HTTPS cert/hash/size/chip/version/image lỗi trên thiết bị | CHƯA CHẠY |
| Cắt nguồn vật lý ở 10/50/90% và các điểm commit | CHƯA CHẠY |
| Corrupt ota_0 -> boot Recovery | CHƯA CHẠY |
| Candidate lỗi -> watchdog/rollback -> Recovery | CHƯA CHẠY |
| Candidate khỏe -> main xác nhận SUCCESS đúng version | CHƯA CHẠY |
| GPIO25 không có xung bật trong ROM/bootloader/reset | CHƯA ĐO |
| So P/Wh với đồng hồ chuẩn, tải có PF khác nhau | CHƯA ĐO |

Chưa nạp firmware mới vào board, chưa thay cấu hình/secret production và chưa
push thay đổi này lên GitHub. Không có domain firmware, tài khoản broker riêng,
khóa riêng thiết bị hoặc kết nối test board được cung cấp để làm E2E.
Quy trình test chi tiết và UART migration ở `OTA_MQTT_PROTOCOL.md`.

## Giới hạn cần giữ rõ

- Dùng manifest HMAC-SHA256 với secret riêng thiết bị + verified HTTPS, không
  phải chữ ký bất đối xứng/secure boot. NVS hiện chưa mã hóa; chưa chống physical attacker.
- Chỉ có một main slot: rollback là về **Recovery**, không có main cũ dự phòng.
- App PENDING_VERIFY lỗi sẽ bị bootloader loại; app đã VALID không tự rollback
  chỉ vì crash. Boot counter xử lý các lần chưa đạt healthy, không bảo đảm lỗi
  lặp sau healthy hoặc trước app_main. Xem phần giới hạn boot validation trong protocol.
- Phần mềm giữ relay OFF từ app_main; cần pulldown phần cứng để bảo đảm cả ROM/reset.
- Giữ nguyên giá cũ 10,000 VND -> 0.9 Wh, không tự đổi sang 130 Wh vì chưa xác nhận.
- Đã thay PF cố định bằng covariance mẫu đo; gain/pha cảm biến và sai lệch ADC
  multiplex phải được hiệu chuẩn trước dùng tính tiền.
- Event/telemetry queues không phải durable ledger. Reboot có thể mất log/DONE
  đang chờ; phiên sạc không tự khôi phục sau mất điện. Không thay đổi nghiệp vụ
  thanh toán sang một giao thức mới khi chưa được yêu cầu.
