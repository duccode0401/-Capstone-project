# Build và chuyển sang firmware SmartCharge một ứng dụng

Firmware dùng ESP-IDF 5.5.1, ESP32-WROOM-32 và flash 4 MB. OTA/Recovery đã bị loại bỏ. Broker cố định là `mqtts://broker.emqx.io:8883`, TLS luôn xác minh bằng certificate bundle của ESP-IDF.

## Build và kiểm thử

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
Set-Location 'D:\-Capstone-project-main\firmware 1.0'
. .\load-esp-idf.ps1
idf.py --version
idf.py -B build-smartcharge fullclean
idf.py -B build-smartcharge build
python tools\verify_builds.py
python -m unittest discover -s tests -v
idf.py -B build-smartcharge size
```

Không dùng `sdkconfig.ota`, không build Recovery. `HEADLESS_TEST_MODE` chỉ dành cho board trần; GPIO25 luôn OFF và không có số đo giả.

## Provision ban đầu

Wi-Fi có thể để trống để ESP32 tự vào Setup Mode. Tạo file local, không commit:

```powershell
New-Item -ItemType Directory -Force provisioning | Out-Null
Copy-Item provision.example.json provisioning\device.json
notepad provisioning\device.json
python tools\provision.py --config provisioning\device.json --out provisioning\device
```

`device-secret.json`, `nvs.csv` và `nvs.bin` chứa bí mật. Backend cần cùng `key_hex` để ký `START_CHARGE`; không đưa key lên GitHub hay MQTT.

## Migration từ layout Recovery + OTA

NVS vẫn ở `0x9000` kích thước `0x4000`; `phy_init` vẫn ở `0xF000`. App mới là factory duy nhất tại `0x10000`, kích thước `0x3F0000`. Không dùng lại lệnh nạp Main cũ tại `0x110000`.

Trước khi nạp: ngắt tải điện, xác nhận relay OFF và đóng serial monitor. Tool dưới đây luôn đọc backup 4 MB trước, không erase toàn flash. Bỏ `--nvs` để giữ nguyên NVS hiện có:

```powershell
python tools\initial_flash.py --port COM3 `
  --backup "provisioning\flash-before-single-app-$(Get-Date -Format yyyyMMdd-HHmmss).bin"
```

Chỉ thêm `--nvs provisioning\device\nvs.bin` khi chủ động thay NVS. Không có lệnh flash nào chạy trong build hoặc test.

## Đổi Wi-Fi bằng điện thoại

Khi chưa có Wi-Fi hoặc kết nối thất bại đủ số lần, LCD hiện SSID `SmartCharge-XXXXXX` và mật khẩu Setup riêng. Kết nối điện thoại vào AP đó, mở `http://192.168.4.1`, chọn Wi-Fi 2.4 GHz và nhập mật khẩu Wi-Fi cùng mật khẩu Setup.

Firmware thử kết nối trước. Chỉ sau khi nhận IP mới ghi Wi-Fi vào NVS. Mật khẩu Wi-Fi cũ không hiển thị lại. Setup Mode mặc định hết hạn sau 10 phút và relay luôn OFF. Có thể đổi timeout/số lần thử trong `Component config -> SmartCharge`.

## Monitor

```powershell
idf.py -B build-smartcharge -p COM3 monitor
```

UART 115200. MQTT tự reconnect; telemetry/heartbeat mỗi 10 giây. QoS 1 có PUBACK và Application ACK. ACK trên broker công cộng chỉ để chẩn đoán, không cấp quyền bật relay. Lệnh sạc phải đúng device, HMAC-SHA256, thời hạn và sequence chống replay.
