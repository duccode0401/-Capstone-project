# Public MQTT bench mode

Firmware kết nối cố định `broker.emqx.io:8883` qua TLS có xác minh CA. Không dùng IP laptop, mDNS, UDP discovery, CA tự tạo, username hoặc password MQTT.

Broker công cộng không tạo biên tin cậy. Chỉ dùng dữ liệu thử nghiệm, không gửi thông tin cá nhân/thanh toán/secrets. Firmware chỉ subscribe `iot/charge_station/v1/orders` khi NVS có HMAC key hợp lệ và vẫn xác minh toàn bộ chữ ký, device, expiry và sequence trước khi bật relay. Bản tin JSON không ký không thể bật tải.

Telemetry, logs, heartbeat, status và ACK giữ topic/schema v1/v2 hiện tại để Django xử lý. Application ACK từ broker công cộng chỉ có giá trị quan sát; không xác nhận thanh toán hay điều khiển phần cứng.
