"""Create LOCAL secret provisioning NVS, or signed release JSON. Does not flash.

Run under ESP-IDF Python. Output belongs outside version control.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from ota_protocol import release, trusted_url


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--image", type=Path)
    p.add_argument("--request-id")
    p.add_argument("--seq", type=int)
    p.add_argument("--manifest-url")
    p.add_argument("--image-url")
    p.add_argument("--bootstrap", action="store_true",
                   help="Authorize first UART-preloaded app, only on initial provisioning")
    a = p.parse_args()
    cfg = json.loads(a.config.read_text(encoding="utf-8-sig"))
    a.out.mkdir(parents=True, exist_ok=True)
    if a.image:
        command, manifest = release(cfg, a.image.read_bytes(), a.request_id, a.seq,
                                    a.manifest_url, a.image_url)
        for name, value in (("command.json", command), ("manifest.json", manifest)):
            (a.out / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        print("Created signed command.json and manifest.json; no credentials printed")
        return
    limits = {"device": 31, "ssid": 32, "password": 64, "broker": 191,
              "mqtt_user": 63, "mqtt_password": 127, "trust_base": 191}
    for field, limit in limits.items():
        if field not in cfg or not isinstance(cfg[field], str) or len(cfg[field].encode()) > limit:
            raise ValueError(f"Missing or oversized {field}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,31}", cfg["device"]):
        raise ValueError("Invalid device ID")
    if (not cfg["broker"].startswith("mqtts://") or not cfg["mqtt_user"] or not cfg["mqtt_password"]
            or any(c in cfg["broker"] for c in "\r\n\t @?#")):
        raise ValueError("Provision an authenticated TLS broker account for this device")
    if "broker.hivemq.com" in cfg["broker"] or not trusted_url(cfg["trust_base"], cfg["trust_base"]):
        raise ValueError("Provision your own trusted broker and HTTPS firmware base")
    cfg.setdefault("key_hex", secrets.token_hex(32))
    if not re.fullmatch(r"[a-fA-F0-9]{64}", cfg["key_hex"]):
        raise ValueError("key_hex must contain 32 bytes in hex")
    text = json.dumps(cfg, separators=(",", ":"), ensure_ascii=True)
    if len(text) >= 4096:
        raise ValueError("Configuration exceeds firmware bound")
    (a.out / "device-secret.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    csv_path = a.out / "nvs.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(("key", "type", "encoding", "value"))
        w.writerow(("sc", "namespace", "", ""))
        w.writerow(("cfg", "data", "string", text))
        if a.bootstrap:
            w.writerow(("state", "data", "string", "bootstrap"))
    generator = Path(os.environ["IDF_PATH"]) / "components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py"
    subprocess.run([sys.executable, str(generator), "generate", str(csv_path),
                    str(a.out / "nvs.bin"), "0x4000"], check=True)
    print("Created secret provisioning files. Do not commit, serve or log these files.")


if __name__ == "__main__":
    main()
