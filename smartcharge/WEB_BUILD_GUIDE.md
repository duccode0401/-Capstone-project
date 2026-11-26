# Hướng dẫn chạy SmartCharge Web trên Windows

## 1. Tạo môi trường Python

Mở PowerShell trong thư mục `smartcharge` chứa `manage.py`. Dùng Python 3.11 trở lên.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Tạo giá trị ngẫu nhiên cho `DJANGO_SECRET_KEY` và dán vào `.env`. Giữ riêng tệp `.env` và các tệp SQLite.

```powershell
.\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

## 2. Chọn chế độ MQTT

### Public Test

Dùng alias chính xác đã cấp cho ESP32. Giá trị dưới đây là ví dụ. Alias gồm tiền tố `SC_TEST_` và đúng 16 ký tự hex viết thường.

```dotenv
MQTT_PUBLIC_TEST_MODE=true
MQTT_PUBLIC_TEST_DEVICE=SC_TEST_0123456789abcdef
MQTT_PUBLIC_TEST_LOGICAL_DEVICE=ESP32_001
```

Subscriber kết nối TLS ẩn danh đến `broker.emqx.io:8883`, nhận ba topic `telemetry`, `logs` và `heartbeat` dưới `iot/charge_station/test/<alias>/`. ACK phát trên `iot/charge_station/test/<alias>/ack`. Payload `device` phải trùng alias. Firmware legacy vẫn dùng được khi gửi telemetry và log lên topic của alias. Chế độ này dùng `db-public-test.sqlite3` và khóa đường gửi lệnh sạc, OTA và xử lý SePay. Broker công cộng chỉ dùng cho thử nghiệm.

### Secure

Điền hostname broker TLS riêng và thông tin đăng nhập backend do quản trị broker cấp.

```dotenv
MQTT_PUBLIC_TEST_MODE=false
MQTT_BROKER=mqtt.example.com
MQTT_PORT=8883
MQTT_USERNAME=smartcharge-backend
MQTT_PASSWORD=THAY_BANG_MAT_KHAU_BACKEND
MQTT_DEVICE_ID=ESP32_001
DEVICE_STALE_SECONDS=30
```

Broker phải có chứng chỉ TLS hợp lệ và ACL giới hạn quyền publish theo vai trò. Backend subscriber cần quyền đọc `iot/charge_station/v1/{telemetry,logs,status,heartbeat}` và `iot/charge_station/v1/ota/status`, cùng quyền ghi `iot/charge_station/v1/ack/+`. ESP32 đăng ký ACK theo Device ID của nó, ví dụ `iot/charge_station/v1/ack/ESP32_001`. Thêm thiết bị phụ trong Django Admin trước khi gửi v2. Thiết bị mặc định lấy từ `MQTT_DEVICE_ID`. Chế độ Secure dùng `db.sqlite3`.

Nếu sử dụng SePay hoặc OTA, điền các biến tương ứng trong `.env.example` theo cấu hình triển khai. Dùng tài khoản broker riêng cho Secure mode và bảo vệ thông tin thanh toán, khóa OTA và chứng chỉ.

## 3. Khởi động Web và subscriber

Chạy từng lệnh từ thư mục `smartcharge`. Sau khi đổi chế độ, chạy lại migration và tạo tài khoản quản trị cho database của chế độ đó.

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py collectstatic --noinput
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

Mở PowerShell thứ hai trong cùng thư mục. Chạy một tiến trình subscriber cho mỗi môi trường.

```powershell
.\.venv\Scripts\python.exe mqtt_subscriber.py
```

- Đăng nhập tại [Django Admin](http://127.0.0.1:8000/admin/).
- Xem sức khỏe thiết bị tại [Device Dashboard](http://127.0.0.1:8000/dashboard/devices/).
- Xem, lọc và phân trang kết quả MQTT tại [Device Logs](http://127.0.0.1:8000/dashboard/logs/). Bộ lọc thời gian dùng giờ Việt Nam (UTC+7). Trang hiển thị tối đa 10.000 kết quả mới nhất trong 7 ngày.

## 4. Kiểm tra với ESP32

1. Bật ESP32 ở chế độ đã chọn. Sau `[MQTT] CONNECTED` và `[MQTT] SUBSCRIBED`, kiểm tra `[RX] TYPE=HEARTBEAT` khoảng 10 giây một lần. Dashboard hiển thị firmware, heap, RSSI và thời điểm heartbeat gần nhất.
2. Với heartbeat hợp lệ, đối chiếu Message ID trong Device Logs và trên ESP32. Subscriber ghi `[DB] COMMIT SUCCESS`, `[TX] ACK SUCCESS` rồi `[MQTT] ACK PUBACK`. Trạng thái `BROKER_CONFIRMED` xác nhận broker đã phản hồi. Kiểm tra log hoặc MQTT client phía ESP32 để xác nhận thiết bị đã nhận ACK.
3. Gửi heartbeat với ID hợp lệ mới và `relay_on` là chuỗi `"false"`. Kết quả phải là `FAILED`, `INVALID_PAYLOAD` và `retryable=false`. Dùng ID có mẫu `0123456789abcdef-00000001` và đúng Device ID để backend có thể định tuyến phản hồi.
4. Gửi lại nguyên payload hợp lệ, gồm cùng Message ID. Kết quả phải là `DUPLICATE` và ACK `SUCCESS`. Đổi một giá trị trên chính payload đó với cùng ID để kiểm tra `MESSAGE_ID_CONFLICT`. Giữ các payload thử nghiệm ở môi trường kiểm thử.
5. Tắt ESP32 hoặc ngắt mạng lâu hơn `DEVICE_STALE_SECONDS`. Dashboard chuyển OFFLINE ở lần làm mới kế tiếp. Thiết bị legacy dùng thời gian hoạt động gần nhất cho đến heartbeat v2 đầu tiên. Dashboard để trống thông số đo của thiết bị HEADLESS.
6. Khi mô phỏng database lỗi tạm thời, kiểm tra ACK `FAILED` với `DATABASE_ERROR` và `retryable=true`. Sau khi database phục hồi, gửi lại nguyên payload. Firmware legacy thiếu Message ID tiếp tục được xử lý theo giao thức cũ.

Bản ghi xử lý phục vụ Device Logs được giữ 7 ngày. Khóa chống trùng được giữ 30 ngày kể từ lần nhận thành công gần nhất của chính bản tin đó. Tiến trình subscriber dọn theo lô. Giữ cửa sổ gửi lại của ESP32 ngắn hơn 30 ngày và tạo Message ID mới cho bản tin mới.

## 5. Khắc phục lỗi

| Dấu hiệu | Kiểm tra |
| --- | --- |
| Chưa có `CONNECTED` | Kiểm tra DNS, kết nối TCP và chứng chỉ TLS. Với broker riêng, kiểm tra hostname, cổng và tài khoản. |
| Có `CONNECTED` nhưng không có `SUBSCRIBED` | Kiểm tra ACL broker và topic. Public Test yêu cầu alias hợp lệ. |
| Đã subscribe nhưng không thấy `RX` | So sánh chế độ, topic và alias ở hai phía. Kiểm tra ESP32 gửi bản tin mới với retain tắt. |
| Có `INVALID_DEVICE`, `UNAUTHORIZED_DEVICE` hoặc `INVALID_MESSAGE_ID` | Kiểm tra Device ID, alias và mẫu Message ID. Bản tin có địa chỉ phản hồi hoặc ID không hợp lệ được ghi lỗi để tra cứu. |
| Có `PUBLISH_FAILED` hoặc ACK đứng ở `QUEUED` | Kiểm tra kết nối và ACL ghi topic ACK. Cho ESP32 gửi lại nguyên payload để nhận ACK mới. |
| Web trống nhưng subscriber nhận bản tin | Kiểm tra cả hai cửa sổ dùng cùng thư mục, `.env` và chế độ. Chạy migration rồi đăng nhập bằng tài khoản có quyền xem telemetry và device log. |

```powershell
Test-NetConnection broker.emqx.io -Port 8883
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test
```

Xem [hướng dẫn OTA từ build đến phục hồi](WEB_OTA_GUIDE.md) để cấu hình Secure mode, xác minh trust base và triển khai application .bin.
