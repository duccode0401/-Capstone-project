# SmartCharge 2.0

Repository chứa hai ứng dụng chính:

- Web Django: [`smartcharge/`](smartcharge/)
- Firmware ESP32-WROOM-32: [`firmware 1.0/`](firmware%201.0/)
- Firmware cũ chỉ để tham chiếu: [`firmware/`](firmware/)

Phiên bản 2.0 sử dụng một Main Application trên ESP32 4 MB; OTA và Recovery đã được loại bỏ. Wi-Fi được cấu hình qua NVS hoặc Setup Mode trên điện thoại. Telemetry, log và heartbeat dùng MQTT TLS công cộng cho mục đích thử nghiệm.

## Chạy Web trên Windows

```powershell
Set-Location smartcharge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Mở PowerShell thứ hai, kích hoạt cùng virtual environment và chạy subscriber:

```powershell
Set-Location smartcharge
.\.venv\Scripts\Activate.ps1
python mqtt_subscriber.py
```

Các biến Public Test tối thiểu trong `.env`:

```dotenv
DJANGO_SECRET_KEY=<chuỗi bí mật cục bộ>
MQTT_PUBLIC_TEST_MODE=true
MQTT_PUBLIC_TEST_DEVICE=SC_TEST_<16 ký tự hex thường>
MQTT_PUBLIC_TEST_LOGICAL_DEVICE=ESP32_001
MQTT_BROKER=broker.emqx.io
MQTT_PORT=8883
MQTT_USERNAME=
MQTT_PASSWORD=
```

Alias phải đúng alias firmware in ra khi provisioning hoặc tại UART `Public route alias=...`. Không commit `.env`, khóa HMAC, mật khẩu Wi-Fi hoặc database local.

## MQTT Public Test

Kết nối dùng `mqtts://broker.emqx.io:8883`, xác minh CA và hostname, không username/password:

```text
iot/charge_station/test/<alias>/telemetry
iot/charge_station/test/<alias>/logs
iot/charge_station/test/<alias>/heartbeat
iot/charge_station/test/<alias>/ack
```

Payload v2 dùng `device=<alias>`, `logical_device=ESP32_001`, `message_id`, `message_type`, `protocol_version=2` và `uptime_ms`. Django chỉ trả Application ACK sau validate và database commit. Public Test không dùng để xác nhận thanh toán hoặc cấp quyền bật relay; lệnh sạc vẫn phải qua HMAC, thời hạn và chống replay.

## Build và provision Firmware

Yêu cầu ESP-IDF 5.5.1:

```powershell
Set-Location 'firmware 1.0'
Set-ExecutionPolicy -Scope Process Bypass -Force
. .\load-esp-idf.ps1
idf.py -B build-smartcharge fullclean
idf.py -B build-smartcharge build
python tools\verify_builds.py
python -m unittest discover -s tests -v
```

Tạo provisioning local:

```powershell
New-Item -ItemType Directory -Force provisioning | Out-Null
Copy-Item provision.example.json provisioning\device.json
notepad provisioning\device.json
python tools\provision.py --config provisioning\device.json --out provisioning\device
```

Nếu SSID để trống, ESP32 mở `SmartCharge-XXXXXX`; kết nối bằng mật khẩu Setup đã tạo rồi truy cập `http://192.168.4.1`. Relay GPIO25 luôn OFF trong Setup Mode.

## Nạp ESP32 lần đầu

Ngắt tải điện, đóng Serial Monitor và backup Flash trước khi chuyển từ layout OTA cũ. Thay `COM3` theo máy:

```powershell
python tools\initial_flash.py --port COM3 `
  --nvs provisioning\device\nvs.bin `
  --backup "provisioning\flash-before-single-app-$(Get-Date -Format yyyyMMdd-HHmmss).bin"
```

Tool ghi bootloader tại `0x1000`, partition table tại `0x8000`, NVS tại `0x9000` khi có `--nvs`, và ứng dụng tại `0x10000`. Tool không erase toàn Flash. Các lần cập nhật source tiếp theo không cần ghi lại NVS:

```powershell
idf.py -B build-smartcharge -p COM3 flash monitor
```

Không chạy `erase-flash` nếu muốn giữ Wi-Fi, alias, mật khẩu Setup và khóa HMAC. Chi tiết xem [`firmware 1.0/FIRMWARE_BUILD_GUIDE.md`](firmware%201.0/FIRMWARE_BUILD_GUIDE.md) và [`smartcharge/WEB_BUILD_GUIDE.md`](smartcharge/WEB_BUILD_GUIDE.md).
