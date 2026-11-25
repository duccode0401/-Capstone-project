# SmartCharge với EMQX và lệnh có chữ ký

Profile NVS `MQTT_PUBLIC_SIGNED_MODE=true` dùng `ESP32_001` và `mqtts://broker.emqx.io:8883`. ESP-IDF xác minh chứng chỉ và hostname bằng CA bundle. MQTT username/password để trống. Lệnh sạc và OTA vẫn cần HMAC-SHA256 bằng khóa riêng của board. Profile MQTT công cộng chỉ xem số đo trước đây vẫn dùng `MQTT_PUBLIC_TEST_MODE` và khóa relay/OTA.

**Ngắt tải vật lý trước khi nạp và thử nghiệm.** Kiểm tra GPIO25 có pulldown ngoài và relay active-high. Firmware đặt GPIO25 LOW lúc khởi động, dừng khi mất phiên Wi‑Fi/MQTT, lỗi ADC hoặc quá 15 phút, và kéo LOW trong ISR task watchdog. Cần đo tín hiệu trên board để kiểm tra thời điểm ngắt, nhất là lúc reset, sụt áp và mất kết nối trong cửa sổ đo ADC.

## Build và nạp qua Windows PowerShell

Trong ESP-IDF PowerShell 5.5.1, mở đúng thư mục `firmware 1.0`:

~~~powershell
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota build
Push-Location recovery
idf.py -B build-recovery -D SDKCONFIG=sdkconfig.recovery build
Pop-Location
python tools/verify_builds.py
python -m unittest discover -s tests -v

New-Item -ItemType Directory -Force provisioning | Out-Null
Copy-Item provision.emqx-signed.example.json provisioning/emqx-signed.json
~~~

Điền SSID và mật khẩu Wi‑Fi 2.4 GHz trong file vừa tạo. Mẫu dùng ngrok làm `trust_base`. Để trống các trường MQTT credentials và bỏ `key_hex` để tool sinh khóa 32 byte một lần. Giữ file đầu ra chứa khóa trong máy. Django cần đọc khóa từ cấu hình bí mật cục bộ `OTA_DEVICE_KEYS_JSON`, ánh xạ `ESP32_001` sang `key_hex` trong `provisioning/emqx-signed/device-secret.json`. Backend ký lệnh sạc dùng cùng khóa, với miền HMAC riêng cho từng loại thông điệp.

~~~powershell
python tools/provision.py --config provisioning/emqx-signed.json --out provisioning/emqx-signed --bootstrap
python tools/initial_flash.py --port COM3 --nvs provisioning/emqx-signed/nvs.bin --backup provisioning/emqx-signed/flash-before.bin
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -p COM3 monitor
~~~

Chỉ gọi installer thủ công khi chuyển layout hoặc provisioning lại. Nó kiểm tra ESP32 4 MiB, sao lưu flash và nạp Recovery ở `0x10000` cùng Main ở `0x110000`. Chọn tên backup chưa tồn tại và giữ backup riêng. Việc ghi lại NVS đặt lại các mốc chống phát lại. Khi cài sạch, dùng khóa mới và đồng bộ lại backend. Khi chuyển từ bản Recovery trước, cần nạp cả Recovery mới để nó hiểu profile NVS này.

## Tương thích Django và giới hạn broker công cộng

Firmware dùng sáu topic `iot/charge_station/v1/{orders,status,telemetry,logs,ota/command,ota/status}`. Dữ liệu telemetry, event logs, DONE và OTA status giữ schema cũ. Các lỗi MQTT/TLS được ghi bằng mã lỗi số, telemetry cũ bị loại khỏi queue và lệnh từ phiên đã mất kết nối bị bỏ.

Django hiện tại chưa chạy đầu cuối với profile này. Nó gửi lệnh sạc dạng `DEVICE PORT AMOUNT` chưa ký, từ chối EMQX trên đường publish và dùng namespace riêng cho chế độ chỉ xem công cộng. Để tiếp tục luồng SePay, backend cần thay payload `orders` bằng JSON đã ký, và kết nối và subscribe đúng namespace `v1`. JSON lệnh sạc gồm:

| Trường | Giá trị |
| --- | --- |
| `device`, `command` | `ESP32_001`, `START_CHARGE` |
| `port` | Số nguyên `1` |
| `order_id` | ID nội bộ không chứa PII, 1–63 ký tự ASCII chữ, số, dấu chấm, gạch ngang hoặc gạch dưới |
| `amount_vnd` | Số nguyên từ 1 tới 10.000.000 |
| `expires_at` | Unix UTC giây, còn hiệu lực và tối đa 300 giây kể từ thời điểm ESP32 nhận |
| `seq` | Số nguyên dương uint32, tăng bền vững theo thiết bị, độc lập với sequence OTA |
| `signature` | HMAC-SHA256 hex của canonical text |

Canonical text theo thứ tự `SC-ORDER-1\n{device}\nSTART_CHARGE\n{port}\n{order_id}\n{amount_vnd}\n{expires_at}\n{seq}`, không có LF cuối. Xem `order_text()` và `sign()` trong [tools/ota_protocol.py](tools/ota_protocol.py). Backend cần giữ nguyên sequence khi thử gửi lại cùng order và không retain lệnh. Firmware lưu sequence vào NVS thành công trước khi bật relay. Phiên dừng do lỗi không phát DONE và không tự khởi động lại.

`START_OTA`, manifest, HTTPS verification, SHA256, version, replay và rollback vẫn dùng giao thức hiện có. EMQX xác thực máy chủ TLS nhưng cho bên thứ ba đọc hoặc giả mạo bản tin. HMAC bảo vệ lệnh đến thiết bị. Django hiện vẫn tin dữ liệu DONE/OTA status, còn URL manifest hiện tại mang token quyền tải trong payload MQTT. Trước khi dùng Web cho giao dịch thật trên broker công cộng, backend phải xác thực status và bảo vệ quyền tải khỏi người nghe broker. Không đưa token secret, dữ liệu khách hàng hay thông tin giao dịch thật vào broker công cộng. Chỉ thử với tải đã ngắt, dữ liệu mô phỏng và release test.
