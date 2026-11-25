# OTA server setup

For the assigned ngrok domain on Windows, follow [the local guide](LOCAL_NGROK_OTA_SETUP.md).

## Environment

Copy `.env.example` to `.env` and configure at least:

```dotenv
FIRMWARE_HTTPS_BASE_URL=https://ota.example.com
OTA_DEVICE_KEYS_JSON={"ESP32_001":"<64 lowercase hex characters>"}
MQTT_BROKER=mqtt.example.com
MQTT_PORT=8883
MQTT_USERNAME=smartcharge-backend
MQTT_PASSWORD=<strong unique password>
```

Use an HTTPS origin. Set the ESP32 `trust_base` to that same origin with a
trailing slash. The device's `key_hex` must match its entry in
`OTA_DEVICE_KEYS_JSON`. Use a different random 32-byte key per device. Do not
commit keys, MQTT credentials or `.env`.

The current firmware enables OTA only when its NVS configuration contains an
`mqtts://` broker, MQTT username/password, HTTPS `trust_base`, and 64-character
lowercase `key_hex`. Its certificate bundle verifies a publicly trusted TLS
certificate and requires correct wall-clock time.

## Database and processes

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py check
```

Run Django and exactly one subscriber as separate supervised processes:

```powershell
python manage.py runserver
python mqtt_subscriber.py
```

For production, replace `runserver` with a production WSGI server. Never import
or start `mqtt_subscriber.py` inside WSGI workers, because every worker would then
receive and process the same MQTT messages.

## HTTPS reverse proxy

Use a valid public certificate whose hostname matches `FIRMWARE_HTTPS_BASE_URL`.
An example Nginx front end is:

```nginx
server {
    listen 443 ssl http2;
    server_name ota.example.com;
    ssl_certificate     /etc/letsencrypt/live/ota.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ota.example.com/privkey.pem;

    client_max_body_size 4m;

    location /firmware/ {
        access_log off; # capability token is in the URL path
        proxy_pass http://127.0.0.1:8000;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

Also disable or redact the application server access log for `/firmware/`; the
reverse-proxy rule alone does not alter a Gunicorn/Uvicorn request log.

Production firmware files live under `media-private/`; do not expose this folder
with a public `/media/` alias. Downloads go through the capability check and are
streamed with `Content-Length`, `application/octet-stream`, `nosniff`, and HTTP
Range support. Object storage may replace local storage if it keeps private
objects and issues equivalently short-lived HTTPS access. Django development
server must not serve production firmware.

## Operations and retention

`DEVICE_STALE_SECONDS` defaults to 30 seconds. OTA status timeout defaults to
900 seconds and the capability TTL to 7200 seconds. Keep one subscriber running
under systemd, Docker Compose, Supervisor, or another process manager and alert
on repeated disconnect/database errors. Back up the database and private firmware
directory together. Configure broker ACLs per device; TLS without authentication
does not prevent another broker client from publishing forged commands/status.
