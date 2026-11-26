# SmartCharge Web

Django dashboard, payment workflow, device telemetry/log history and a standalone MQTT subscriber. Web OTA, mDNS and the local Mosquitto/CA toolchain have been removed. Existing OTA database tables remain as read-only legacy data so upgrades do not delete history.

## MQTT modes

`MQTT_PUBLIC_TEST_MODE=true` is the safe development default. It uses TLS with the operating system's public CA store and the device-specific topics:

```text
iot/charge_station/test/<SC_TEST_alias>/telemetry
iot/charge_station/test/<SC_TEST_alias>/logs
iot/charge_station/test/<SC_TEST_alias>/heartbeat
iot/charge_station/test/<SC_TEST_alias>/ack
```

Public mode accepts only idle telemetry, allow-listed device logs and heartbeat data. It cannot create payment orders, mark charging complete, publish relay commands or start a charging session. Never use an anonymous public broker for real charging operations.

For an operational deployment set `MQTT_PUBLIC_TEST_MODE=false`, use a private TLS broker and unique backend credentials. The backend deliberately rejects known public brokers in this mode. Current firmware-compatible operational topics remain `iot/charge_station/v1/telemetry`, `logs`, `heartbeat`, `status`, device ACK and orders topics.

Copy `.env.example` to `.env` and set the broker through `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` and optional `MQTT_CA_FILE`. Leave `MQTT_CA_FILE` empty for a certificate issued by a public CA. Secrets and databases must not be committed.

## Run on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Set MQTT_PUBLIC_TEST_DEVICE in .env to the provisioned SC_TEST_<16 hex> alias.
.\scripts\Start-SmartCharge.ps1
```

The launcher performs Django checks, verifies migrations, collects static files, performs a harmless MQTT TLS/QoS 1 round trip, and starts exactly one Django process plus one subscriber. It binds to `0.0.0.0:8000` by default and prints current private-LAN URLs; use `-LoopbackOnly` to bind only to `127.0.0.1`. Django dynamically refreshes allowed private IPv4 hosts, so no Wi-Fi IP is hard-coded.

For Ngrok add its hostname to `DJANGO_EXTRA_HOSTS` and its full HTTPS origin to `DJANGO_CSRF_TRUSTED_ORIGINS`. Example: `DJANGO_EXTRA_HOSTS=example.ngrok-free.app` and `DJANGO_CSRF_TRUSTED_ORIGINS=https://example.ngrok-free.app`.

## Checks

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
python manage.py collectstatic --noinput
python manage.py mqtt_health --ping
```

WhiteNoise serves collected CSS/JavaScript when `DEBUG=false`. Subscriber reconnect delay is exponential from 1 to 30 seconds. MQTT audit payloads retain only validated measurements/status fields and redact free-form messages.
