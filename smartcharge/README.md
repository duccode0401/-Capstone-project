# SmartCharge backend

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
