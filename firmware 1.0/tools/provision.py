"""Create initial SmartCharge NVS without flashing or printing secrets."""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    source = json.loads(args.config.read_text(encoding="utf-8-sig"))
    device = source.get("device", "ESP32_001")
    ssid = source.get("ssid", "")
    password = source.get("password", "")
    setup_password = source.get("setup_password") or secrets.token_urlsafe(9)[:12]
    key_hex = source.get("key_hex") or secrets.token_hex(32)
    if not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,31}", device):
        raise ValueError("Invalid device ID")
    for name, value, limit in (("ssid", ssid, 32), ("password", password, 64)):
        if not isinstance(value, str) or len(value.encode()) > limit:
            raise ValueError(f"Invalid {name}")
    if not isinstance(setup_password, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,16}", setup_password):
        raise ValueError("setup_password must be 8-16 safe characters")
    if not isinstance(key_hex, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", key_hex):
        raise ValueError("key_hex must contain 32 bytes in hexadecimal")
    key_hex = key_hex.lower()
    legacy = {"device": device, "ssid": ssid, "password": password, "key_hex": key_hex}
    args.out.mkdir(parents=True, exist_ok=True)
    secret = dict(legacy, setup_password=setup_password,
                  broker="mqtts://broker.emqx.io:8883")
    (args.out / "device-secret.json").write_text(json.dumps(secret, indent=2), encoding="utf-8")
    with (args.out / "nvs.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(("key", "type", "encoding", "value"))
        writer.writerow(("sc", "namespace", "", ""))
        writer.writerow(("cfg", "data", "string", json.dumps(legacy, separators=(",", ":"))))
        if ssid:
            writer.writerow(("wifi_ssid", "data", "string", ssid))
            writer.writerow(("wifi_pass", "data", "string", password))
        writer.writerow(("setup_pass", "data", "string", setup_password))
    generator = Path(os.environ["IDF_PATH"]) / "components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"
    subprocess.run([sys.executable, str(generator), "generate", str(args.out / "nvs.csv"),
                    str(args.out / "nvs.bin"), "0x4000"], check=True)
    print("Created nvs.bin and local device-secret.json. No secret was printed; do not commit the output directory.")


if __name__ == "__main__":
    main()
