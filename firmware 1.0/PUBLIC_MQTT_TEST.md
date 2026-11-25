# Test ESP32 và Django qua EMQX công cộng

Chế độ này dành cho ESP32 4 MiB trên bàn test. **Ngắt tải điện vật lý khỏi trạm trước khi nạp hoặc chạy.** GPIO25 được giữ OFF trong firmware. ESP32 kết nối TLS có xác minh chứng chỉ tới `broker.emqx.io:8883`. Broker công cộng chỉ nhận từ firmware số đo và mã sự kiện với alias test ngẫu nhiên. Bất kỳ ai trên broker cũng có thể đọc hoặc giả mạo dữ liệu test, nên chỉ dùng số liệu thử nghiệm.

## 1. Build và tạo NVS

Trong ESP-IDF PowerShell 5.5.1, mở thư mục `firmware 1.0`:

```powershell
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota build
Push-Location recovery
idf.py -B build-recovery -D SDKCONFIG=sdkconfig.recovery build
Pop-Location
python tools/verify_builds.py
New-Item -ItemType Directory -Force provisioning | Out-Null
Copy-Item provision.public-test.example.json provisioning/public-test.json
```

Mở `provisioning/public-test.json`, điền tên và mật khẩu Wi-Fi 2.4 GHz. Giữ địa chỉ EMQX trong mẫu. `trust_base` cần có dạng HTTPS với dấu `/` cuối. URL trong mẫu đủ để chạy thử vì chế độ này khóa OTA. Bỏ trống MQTT username/password, HMAC và alias thiết bị. Sau đó chạy:

```powershell
python tools/provision.py --config provisioning/public-test.json --out provisioning/public-test --bootstrap
```

Ghi lại dòng `MQTT_PUBLIC_TEST_DEVICE=SC_TEST_...` để dùng ở bước 3. Giữ thư mục provisioning trên máy vì có mật khẩu Wi-Fi.

## 2. Nạp USB lần đầu

Sau khi ngắt tải và xác nhận relay OFF, chạy thủ công lệnh dưới đây. Sửa COM3 nếu board dùng cổng khác và chọn tên backup mới nếu đường dẫn đã tồn tại.

```powershell
python tools/initial_flash.py --port COM3 --nvs provisioning/public-test/nvs.bin --backup provisioning/public-test/flash-before.bin
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -p COM3 monitor
```

Installer sao lưu flash trước khi nạp Recovery ở `0x10000` và Main ở `0x110000`. Giữ bản sao lưu riêng vì có thể chứa cấu hình cũ.

## 3. Chạy Web và subscriber

Trong thư mục `smartcharge`, cài `requirements.txt` vào môi trường Python nếu đây là lần đầu chạy Web. Đặt hai dòng sau trong `smartcharge/.env` cùng cấp với `manage.py`, dùng đúng alias do tool in:

```dotenv
MQTT_PUBLIC_TEST_MODE=true
MQTT_PUBLIC_TEST_DEVICE=SC_TEST_0123456789abcdef
```

Trong terminal Web với môi trường Python đã kích hoạt:

```powershell
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Database của chế độ test là `db-public-test.sqlite3`. Chỉ tạo superuser một lần. Trong terminal thứ hai, cùng thư mục và môi trường Python:

```powershell
python mqtt_subscriber.py
```

Đăng nhập tại [Django admin](http://127.0.0.1:8000/admin/), rồi mở [Logs](http://127.0.0.1:8000/dashboard/logs/) và [Devices](http://127.0.0.1:8000/dashboard/devices/). ESP32 gửi mẫu ADC hợp lệ khoảng mỗi 10 giây. Khi đã ngắt tải, dòng, công suất và Wh có thể bằng 0. Subscriber chỉ lưu telemetry IDLE và sự kiện đã định nghĩa cho alias đã chọn.

Để quay lại OTA_SECURE, tắt cờ trên Web và provision lại ESP32 với broker TLS riêng, tài khoản MQTT, HMAC và HTTPS tin cậy theo [quy trình OTA](OTA_MQTT_PROTOCOL.md).
