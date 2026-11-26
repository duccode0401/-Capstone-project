# SmartCharge backend

Để xem telemetry và log từ ESP32 trên bàn test mà không cần tài khoản MQTT,
dùng [chế độ EMQX công cộng](../firmware%201.0/PUBLIC_MQTT_TEST.md).

[Hướng dẫn chạy Web và subscriber bằng PowerShell](WEB_BUILD_GUIDE.md)

## Khởi động

Tạo môi trường và cài dependency (chỉ cần làm lần đầu):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
```

Điền `DJANGO_SECRET_KEY` và `SEPAY_API_TOKEN` thật vào `.env`. Sau đó mở hai
PowerShell riêng:

```powershell
# Cửa sổ 1: Django
.\.venv\Scripts\Activate.ps1
python manage.py runserver
```

```powershell
# Cửa sổ 2: đúng một MQTT subscriber
.\.venv\Scripts\Activate.ps1
python mqtt_subscriber.py
```

MQTT subscriber là process độc lập, không tự chạy cùng `runserver`. Không mở
nhiều subscriber vì broker có thể giao cùng một status message cho từng process.
Subscriber dùng TLS tới `broker.hivemq.com:8883` và subscribe
`iot/charge_station/v1/status`.

## Kiểm tra bản tin DONE từ ESP32

Firmware giữ nguyên payload:

```json
{"device":"ESP32_001","port":1,"status":"DONE"}
```

Publish payload trên vào topic `iot/charge_station/v1/status`. Log subscriber
phải hiện session được chuyển sang `done`. Có thể xác nhận trong Django shell:

```powershell
python manage.py shell -c "from myapp.models import ChargingSession; print(list(ChargingSession.objects.values('id','status','device__device_id','port_no').order_by('-id')[:10]))"
```

Payload DONE hiện không có `session_id`. Backend chỉ có thể chọn session hoạt
động mới nhất của đúng device/port; nó không thể phân biệt tuyệt đối một message
cũ bị gửi trễ sau khi cổng đã được gán cho phiên kế tiếp.

## Kiểm tra backend

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

## Chạy Web và Mosquitto local trên Windows

Từ thư mục Web, dùng môi trường Python hiện có. Script cấu hình broker dùng hai tài khoản đã tạo cùng CA và chứng chỉ hiện có trong thư mục C:\SmartChargeMQTT. Script sao lưu ACL và cấu hình trước khi cập nhật. Listener TLS dùng loopback, còn Web có thể mở trên LAN. ESP32 trên thiết bị khác cần cấu hình kết nối riêng nếu muốn truy cập broker qua mạng.

```powershell
if (!(Test-Path '.\.venv\Scripts\python.exe')) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Mở PowerShell với quyền Administrator để cấu hình Windows Service. Điền mật khẩu tài khoản backend trong PowerShell thường. Lệnh chỉ ghi vào .env cục bộ sau khi xác minh kết nối.

```powershell
.\scripts\Configure-LocalMosquitto.ps1 -AllowWebLan
```

```powershell
.\.venv\Scripts\python.exe -m scripts.set_local_mqtt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py mqtt_health --ping --check-rejections
.\scripts\Start-SmartCharge.ps1
```

Launcher chạy collectstatic, kiểm tra Mosquitto và xác minh Web phục vụ static qua WhiteNoise. Địa chỉ localhost là http://127.0.0.1:8000/. Launcher in thêm địa chỉ LAN đang dùng. Django cập nhật các IP riêng trên giao diện đang hoạt động khi nhận request, giữ localhost và hostname Ngrok từ cấu hình hiện có. Dùng cùng origin khi đăng nhập. Tùy chọn -AllowWebLan tạo quy tắc Firewall TCP 8000 giới hạn vào Python của Web và subnet cục bộ. Dùng tùy chọn -LoopbackOnly nếu chỉ làm việc trên laptop.

Kiểm tra log Web và subscriber trong thư mục .run. Probe MQTT trao đổi trên smartcharge/local/health/ với ID ngẫu nhiên. Đường dẫn CA có thể chỉnh bằng MQTT_CA_FILE cho cấu hình TLS riêng khác. Thông tin thanh toán, cấu hình HMAC và URL OTA vẫn được quản lý độc lập.
