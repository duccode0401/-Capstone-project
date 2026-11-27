# Mosquitto TLS trên Windows

Broker SmartCharge dùng **mqtts://192.168.1.104:8883**. Mosquitto chỉ lắng nghe trên IP Wi-Fi này và loopback IPv4. Firewall giới hạn nguồn ở **192.168.1.0/24** trên giao diện Wi-Fi. Giữ cổng 8883 trong LAN, tránh port forwarding, DMZ và đường hầm công khai.

| Tệp | Đường dẫn trên máy broker |
|---|---|
| Cấu hình Windows Service | C:\Program Files (x86)\Mosquitto\mosquitto.conf |
| Cấu hình SmartCharge | C:\SmartChargeMQTT\config\smartcharge.conf |
| ACL | C:\SmartChargeMQTT\config\acl |
| Root CA công khai cho client | C:\SmartChargeMQTT\pki\root-ca.pem |
| Bản sao lưu cấu hình | C:\SmartChargeMQTT\backups |

Tệp cấu hình của Windows Service nạp thư mục cấu hình SmartCharge qua `include_dir`. Bảo vệ `passwords.txt`, khóa TLS và khóa CA. Chỉ chuyển tệp `root-ca.pem` cho máy chạy Django và máy build firmware. Bản sao lưu của script bao gồm cấu hình và ACL, cùng thông tin về sự hiện diện của rule firewall. Khóa và tệp mật khẩu cần được sao lưu bảo mật riêng.

## Phân quyền MQTT

Mọi topic bên dưới có tiền tố `iot/charge_station/v1/`.

| Topic | ESP32_001 | smartcharge_backend |
|---|---|---|
| `telemetry` | Publish | Subscribe |
| `logs` | Publish | Subscribe |
| `status` | Publish | Subscribe |
| `ota/status` | Publish | Subscribe |
| `orders` | Subscribe | Publish |
| `ota/command` | Subscribe | Publish |

Backend cập nhật thời điểm nhìn thấy thiết bị từ dữ liệu nhận được. Firmware gửi kết quả phiên sạc trên `status` và trạng thái lệnh OTA trên `ota/status`. Giao thức hiện tại dùng MQTT PUBACK ở tầng giao vận và sáu topic trên. Các topic dùng chung nên ACL cấp quyền theo tài khoản và chiều truyền, còn logic dự án xử lý trường `device` trong payload.

## Web và MQTT subscriber

Trong `smartcharge/.env` riêng của môi trường triển khai, đặt:

~~~dotenv
MQTT_BROKER=192.168.1.104
MQTT_PORT=8883
MQTT_USERNAME=smartcharge_backend
MQTT_PUBLIC_TEST_MODE=false
~~~

Đặt `MQTT_PASSWORD` bằng mật khẩu tài khoản backend trong cấu hình bí mật tại máy. Khởi động Django và MQTT subscriber từ môi trường Python của dự án. Cả hai tiến trình phải tin CA riêng vì cấu hình MQTT hiện tại gọi `ssl.create_default_context()`. Trong mỗi cửa sổ PowerShell chạy tiến trình, đặt đường dẫn PEM trước khi khởi động:

~~~powershell
$env:SSL_CERT_FILE = 'C:\SmartChargeMQTT\pki\root-ca.pem'
python manage.py runserver
~~~

Dùng lệnh `python mqtt_subscriber.py` trong cửa sổ còn lại, sau khi đặt cùng biến môi trường. Nếu dùng Windows Service hoặc scheduler cho Django, cấu hình biến môi trường cho tài khoản quản lý tiến trình. Trình duyệt sử dụng Web Django và giữ thông tin đăng nhập MQTT ở backend.

## ESP32 và Recovery

Dùng nhánh mã nguồn `firmware 1.0`. Main và Recovery cùng sử dụng `components/sc_core/sc_network.c`, nơi MQTT đang gọi `esp_crt_bundle_attach`. Trên máy build, sao chép Root CA công khai vào thư mục build riêng. Trong cấu hình ESP-IDF của **cả hai target**, bật `CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE` rồi đặt `CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE_PATH` trỏ tới PEM đó. Giữ bundle CA mặc định để các kết nối HTTPS hiện có vẫn xác minh được chứng chỉ và giữ xác minh tên máy chủ. Kiểm tra cấu hình và build của từng target trước khi thực hiện quy trình triển khai firmware đã duyệt.

Dùng các trường trong `firmware 1.0/provision.example.json` để chuẩn bị cấu hình riêng:

- `device` và `mqtt_user` là `ESP32_001`.
- `broker` là `mqtts://192.168.1.104:8883`.
- `mqtt_password` lấy từ cấu hình bí mật của thiết bị.
- Giữ `trust_base` theo endpoint HTTPS dùng cho OTA, cùng HMAC key hiện có.
- Giữ chế độ public MQTT tắt và để thiết bị đồng bộ giờ trước khi kết nối.

Recovery dùng cùng thông tin MQTT và chỉ đăng ký lệnh OTA khi OTA được bật. Mọi thao tác cập nhật firmware hoặc dữ liệu NVS cần theo quy trình triển khai của dự án. Xem [ESP-IDF X.509 Certificate Bundle](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/protocols/esp_crt_bundle.html).

## Quản lý và kiểm tra

Mở PowerShell Administrator tại thư mục `smartcharge`. Phiên bản Windows này dùng OpenSSL đi kèm Git for Windows.

~~~powershell
.\Manage-SmartChargeMQTT.ps1 -Action Check
.\Manage-SmartChargeMQTT.ps1 -Action Backup
.\Manage-SmartChargeMQTT.ps1 -Action Restore -BackupPath 'C:\SmartChargeMQTT\backups\<timestamp>'
~~~

`Install` sao lưu cấu hình trước khi chuyển service sang TLS. `Restore` lưu lại cấu hình hiện tại trước khi khôi phục cấu hình được chọn, rồi khởi động lại service.

Để test MQTT thực tế, chạy từ PowerShell tương tác với Python và `paho-mqtt` 2.1.0. Script hỏi hai mật khẩu tại máy và kiểm tra CA, danh tính TLS, anonymous, mật khẩu sai, publish/subscribe telemetry và chiều publish bị ACL từ chối.

~~~powershell
python .\test_smartcharge_mqtt.py
~~~
