# SmartCharge trên LAN với mDNS và MQTT TLS

## Điều kiện

Chỉ bật LAN trên mạng bạn tin cậy. Windows phải đánh dấu mạng đó là **Private**, Firewall phải bật và mặc định chặn kết nối vào. Không chuyển Wi-Fi công cộng sang Private để chạy SmartCharge. Không mở các cổng này trên router. Kiểm tra và gỡ các rule tự tạo cho phép Mosquitto/Python nhận kết nối rộng hơn LocalSubnet trước khi bật LAN.

Django và subscriber tiếp tục dùng `MQTT_BROKER=localhost`, `MQTT_PORT=8883`, tài khoản MQTT hiện có và `MQTT_CA_FILE` trỏ tới Root CA của broker. Nếu chưa thiết lập, chạy `python scripts/set_local_mqtt.py` trong virtual environment. MQTT LAN vẫn yêu cầu TLS, tài khoản và ACL như broker local.

## Cài và bật LAN lần đầu

Tại thư mục `smartcharge` trong PowerShell **Run as Administrator**:

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$root = 'C:\SmartChargeMQTT'
$issuerKey = Read-Host 'Duong dan private key cua Root CA hien tai'
.\scripts\New-LanMosquittoCertificate.ps1 -CaFile "$root\pki\root-ca.pem" -CaKey $issuerKey -ServerKey "$root\pki\server.key" -OutputFile "$root\pki\server-lan.pem"
.\scripts\Configure-LocalMosquitto.ps1 -AllowMqttLan -AllowWebLan
~~~

Chỉ ký chứng chỉ này một lần với cùng Root CA và server key hiện có. Chứng chỉ có CN `smartcharge.local` và SAN `DNS:smartcharge.local`, `DNS:localhost`, `IP:127.0.0.1`. Nó không chứa IP LAN, nên đổi IP không cần ký lại. Giữ khóa riêng của CA và broker ở nơi hạn chế quyền đọc.

Mosquitto vẫn chạy bằng Windows Service. Khi bật LAN, nó lắng nghe IPv4 cổng 8883. Firewall giới hạn rule cho phép tới LocalSubnet trên Private, với rule chặn Public và Domain. Web TCP 8000 và mDNS UDP 5353 được áp chính sách tương ứng. Script giữ ACL đang dùng khi bật MQTT LAN. Để đưa broker về loopback, chạy lại cấu hình với tham số mặc định.

## Khởi động Web và mDNS

Trong PowerShell thường, sau khi Django đã migrate:

~~~powershell
.\scripts\Start-SmartCharge.ps1 -EnableMdns
~~~

Launcher xác minh MQTT local và TLS hostname trước khi bật mDNS. Terminal in URL Web theo IPv4 LAN hiện tại. Điện thoại phải vào cùng mạng Private và dùng IP được in. Nếu thiết bị hỗ trợ mDNS, có thể mở `http://smartcharge.local:8000/`. URL Web dùng HTTP và chỉ dành cho LAN tin cậy.

Service quảng bá `SmartCharge._secure-mqtt._tcp.local.` trỏ về `smartcharge.local.:8883`. Mỗi khoảng 5 giây nó kiểm tra địa chỉ IPv4 và profile. Khi đổi địa chỉ hoặc mạng Wi‑Fi, service bỏ bản ghi cũ và đăng ký lại trên interface Private hiện tại. Nếu mất Private LAN thì dừng quảng bá. Bản ghi có TTL 20 giây trong trường hợp không gửi được gói thu hồi.

Xem thay đổi địa chỉ trong một terminal khác:

~~~powershell
Get-Content .\.run\mdns.stderr.log -Wait
~~~

Để chỉ chạy local, dùng `.\scripts\Start-SmartCharge.ps1 -LoopbackOnly`.

## Kiểm tra

~~~powershell
.\.venv\Scripts\python.exe -m unittest myapp.test_mdns -v
.\.venv\Scripts\python.exe manage.py mqtt_health
~~~

Trên máy khác cùng LAN, dùng trình duyệt mDNS để kiểm tra service và IPv4 nhận được. Thử tên `smartcharge.local` với MQTT TLS, Root CA hiện có và tài khoản được cấp. Đổi Wi-Fi hoặc cấp lại IP rồi so địa chỉ mới trong log và trình duyệt mDNS. Nếu không thấy service, kiểm tra profile Private, AP isolation và multicast UDP 5353. Máy khác kết nối Web bằng IP hiện tại kể cả khi thiết bị đó không phân giải được tên .local.

Thiết lập này áp dụng cho Server. ESP32 và Recovery vẫn dùng hostname/broker đã provision hiện có.
