"""Verify the broker credential before updating the ignored local .env."""
from getpass import getpass
from pathlib import Path
import ssl

from dotenv import set_key
from myapp.mqtt_health import roundtrip

root = Path(__file__).resolve().parents[1]
ca = Path(r"C:\SmartChargeMQTT\pki\root-ca.pem")
password = getpass("MQTT password for smartcharge_backend: ")
if not password:
    raise SystemExit("No password entered. .env was not changed.")
config = {
    "hostname": "localhost",
    "port": 8883,
    "tls": ssl.create_default_context(cafile=str(ca)),
    "auth": {"username": "smartcharge_backend", "password": password},
}
try:
    roundtrip(config, credential_check=True)
except (OSError, ValueError) as exc:
    raise SystemExit(f"{exc}. .env was not changed.") from None
for key, value in {
    "MQTT_PUBLIC_TEST_MODE": "false",
    "MQTT_BROKER": "localhost",
    "MQTT_PORT": "8883",
    "MQTT_USERNAME": "smartcharge_backend",
    "MQTT_PASSWORD": password,
    "MQTT_CA_FILE": str(ca),
}.items():
    set_key(root / ".env", key, value)
print("Local TLS credentials verified and saved to .env.")
