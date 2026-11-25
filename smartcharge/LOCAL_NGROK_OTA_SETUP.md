# Chạy SmartCharge OTA trên Windows qua ngrok

Dùng môi trường này để thử nghiệm có kiểm soát. Django development server phục vụ trên
`http://127.0.0.1:8000` với DEBUG hiện có. Qua ngrok, Django chỉ nhận GET cho manifest
và binary theo URL riêng của OTA job. Mở Admin, dashboard và thanh toán qua địa chỉ local.

## 1. Cấu hình môi trường

Mở PowerShell trong thư mục chứa `manage.py`. Nếu chưa có `.env`, sao chép từ
`.env.example`. Nếu đã có, giữ các giá trị Django và SePay đang dùng rồi bổ sung các
dòng dưới đây bằng cấu hình thật. Không đưa `.env`, file cấp phát, ảnh NVS hoặc
URL tải có token vào Git, log chia sẻ hay ảnh chụp màn hình.

```dotenv
FIRMWARE_HTTPS_BASE_URL=https://capillary-sled-monogamy.ngrok-free.dev
MQTT_BROKER=<hostname-broker-rieng>
MQTT_PORT=8883
MQTT_USERNAME=<tai-khoan-backend>
MQTT_PASSWORD=<mat-khau-backend>
OTA_DEVICE_KEYS_JSON={"ESP32_001":"<key_hex-cua-thiet-bi>"}
```

Dùng hostname thuần trong `MQTT_BROKER`. Đặt `DJANGO_SECRET_KEY` riêng cho máy này.
Mặc định token tải tồn tại tối đa 7200 giây và job hết hạn sau 900 giây
không có cập nhật tiến độ. Telemetry phải mới trong 30 giây.

## 2. Đồng bộ khóa với ESP32

- Với ESP32 đã provision, lấy `key_hex` từ bản cấp phát đã lưu trong kho secrets và
  điền đúng mục `ESP32_001`. Khóa là 32 byte biểu diễn bằng 64 ký tự hex chữ thường.
- Với thiết bị chưa provision, nhờ người vận hành dùng quy trình hiện có trong
  `../firmware/OTA_MQTT_PROTOCOL.md`. Công cụ `tools/provision.py` tạo khóa riêng
  khi bỏ trống `key_hex`. Lưu `device-secret.json` trong kho secrets.
- Kiểm tra NVS của thiết bị được cấp `trust_base` là
  `https://capillary-sled-monogamy.ngrok-free.dev/`, kể cả dấu `/` cuối.
  Web dùng origin ở trên. Đồng bộ đồng hồ ESP32 để xác minh chứng chỉ.
- Giữ DB và thư mục `media-private` khi backup. Nếu khôi phục DB cũ, phải đối
  chiếu `ota_sequence` với replay floor đã lưu trong NVS trước khi phát lệnh tiếp.

## 3. Cấu hình broker MQTT TLS

Dùng broker riêng có TLS trên 8883, chuỗi CA công khai phù hợp với bundle ESP32,
xác thực MQTT và chính sách mặc định từ chối. Trong NVS cấu hình cùng hostname và port
theo dạng `mqtts://<hostname>:8883` với tài khoản riêng của thiết bị. Cấp quyền theo
các suffix dưới đây, đều có tiền tố `iot/charge_station/v1/`.

| Tài khoản | Publish | Subscribe |
| --- | --- | --- |
| Backend | `orders`, `ota/command` | `ota/status`, `telemetry`, `logs`, `status` |
| ESP32 | `ota/status`, `telemetry`, `logs`, `status` | `orders`, `ota/command` |

Các topic trạng thái dùng chung và payload trạng thái hiện không có HMAC.
Dùng broker dành riêng cho trạm thử nghiệm hoặc cơ chế broker ràng buộc trường
`device` với tài khoản publish nếu có nhiều trạm. Bản tin MQTT retained bị
subscriber bỏ qua. Không cấp quyền publish cho tài khoản quan sát.

## 4. Chạy Django

```powershell
# Tạo venv bằng python -m venv .venv nếu chưa có
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py check
# Chạy createsuperuser nếu chưa có tài khoản quản trị
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

## 5. Chạy subscriber

Mở PowerShell thứ hai tại cùng thư mục. Chỉ chạy một tiến trình subscriber.

```powershell
.\.venv\Scripts\python.exe mqtt_subscriber.py
```

Xem log kết nối và lỗi ACL. Khi trạm online, xem
`http://127.0.0.1:8000/dashboard/devices/` và `/dashboard/logs/` bằng tài khoản staff
có quyền xem telemetry và log. Màn hình thiết bị làm mới mỗi 10 giây.
Firmware hiện tại báo version qua OTA SUCCESS, nên trước lần OTA được xác
nhận đầu tiên, version trên dashboard có thể để trống.

## 6. Chạy ngrok

Trong PowerShell thứ ba, dùng ngrok agent đã đăng nhập bằng tài khoản sở hữu domain.

```powershell
ngrok http http://127.0.0.1:8000 --url https://capillary-sled-monogamy.ngrok-free.dev --inspect=false
```

Lần đầu, đăng ký authtoken bằng hướng dẫn trong tài khoản ngrok. Giữ Host gốc
và tắt HTTP inspection để tránh ghi URL chứa token tải. Dùng endpoint HTTPS
và giữ response nguyên dạng, không bật tính năng nén.

## 7. Kiểm tra endpoint HTTPS

Dùng User-Agent mặc định của ESP-IDF 5.5.1 để thử đúng kiểu client của thiết bị.

```powershell
$base = 'https://capillary-sled-monogamy.ngrok-free.dev'
$agent = 'ESP32 HTTP Client/1.0'
curl.exe --http1.1 -sS -A $agent -D - -o NUL "$base/admin/"
curl.exe --http1.1 -sS -A $agent -D - -o NUL "$base/api/check-payment/"
$invalid = 'x' * 43
curl.exe --http1.1 -sS -A $agent -D - -o NUL "$base/firmware/manifest/ota-test/$invalid/"
```

Hai URL đầu phải trả 404 từ Django. Manifest có token sai trả 403. Nếu response
có `Ngrok-Error-Code: ERR_NGROK_3200`, kiểm tra agent và domain trong tài khoản.
Theo ngrok, HTML interstitial trên Free áp dụng cho trình duyệt. ESP32 dùng
User-Agent riêng. Khi tạo job, Django dùng cùng User-Agent và xác minh cả
manifest lẫn binary qua HTTPS trước khi publish. Response phải là HTTP 200, có
`Content-Length`, đúng loại và SHA-256; HTML, redirect, gzip hoặc chunked đều
làm kiểm tra thất bại. Nếu interstitial vẫn xuất hiện với User-Agent ESP32,
dùng gói ngrok bỏ interstitial hoặc endpoint HTTPS tương thích và đồng bộ lại
`trust_base` theo quy trình cấp phát, giữ nguyên xác minh TLS.
[Giới hạn ngrok Free](https://ngrok.com/docs/pricing-limits/free-plan-limits#removing-the-interstitial-page)

## 8. Upload và duyệt firmware

Vào `http://127.0.0.1:8000/admin/` → **Firmware artifacts → Add**. Chọn application
`smartcharge.bin`, type **SmartCharge main**, chip **esp32** và đúng version trong
image, ví dụ `1.1.0`. Bản mới phải lớn hơn version đang chạy. Kiểm tra SHA-256
và nguồn build rồi duyệt. Recovery chỉ có thể lưu làm catalogue, còn bootloader
và partition table không phải application image cho luồng này.

## 9. Tạo OTA job

Đợi telemetry mới và trạm rảnh, sau đó vào **OTA jobs → Add**, chọn
`ESP32_001` và bản đã duyệt, tick xác nhận rồi lưu. Tài khoản cần quyền
**Can add OTA job**, **Can view OTA job** và **Can deploy approved firmware**.
Django kiểm tra HTTPS sau khi lưu job rồi mới publish QoS 1 với `retain=false`.
`PENDING` nghĩa là chưa có phản hồi trạng thái từ thiết bị. Xem
`command_sent_at` để biết backend đã nhận xác nhận publish từ broker.

## 10. Xác nhận và xử lý lỗi

- Theo dõi **OTA jobs**, **Device logs** và dashboard. Có thể thấy
  `ACCEPTED`, `REBOOTING`, `RECOVERING`, `DOWNLOADING`, `VERIFYING`,
  `INSTALLING` rồi `REBOOTING` lần nữa. Trạng thái trung gian có thể mất khi thiết
  bị ngắt MQTT. Chỉ `SUCCESS` với đúng request, version và 100% từ firmware sau
  boot health check xác nhận cập nhật. Kiểm tra telemetry quay lại sau reboot.
- Nếu HTTPS verification thất bại, kiểm tra agent, hostname, loại nội dung,
  Content-Length và file trong `media-private`. ESP32 phải tin được chuỗi CA qua
  bundle của nó. Kiểm tra đồng hồ và log thiết bị nếu PC tải được nhưng ESP32 gặp
  lỗi TLS.
- Nếu không có trạng thái, kiểm tra ACL, HMAC key, `trust_base`, version hiện tại,
  replay floor và log subscriber. Nếu publish chưa được xác nhận, job được giữ
  active đến khi có phản hồi hoặc timeout để xử lý trường hợp broker nhận lệnh
  nhưng kết nối mất trước PUBACK.
- Nếu Recovery báo `FAILED` trong khi cài, job hiển thị `RECOVERING` cùng lỗi
  `FAILED` để giữ URL hoạt động cho retry. Đối chiếu event log để phân biệt lỗi
  tải tạm thời và ba lần boot thất bại. Job thất bại khi hết hạn hoặc quá thời
  gian chờ. Nếu Recovery không phát telemetry, tiếp tục quy trình bảo trì thiết
  bị đã nêu trong tài liệu firmware. Web yêu cầu telemetry mới để bắt đầu job.
