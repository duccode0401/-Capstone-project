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
from urllib.parse import urlsplit
from ota_protocol import release, trusted_url


PUBLIC_BROKER = "mqtts://broker.emqx.io:8883"
PUBLIC_HOSTS = ("broker.hivemq.com", "broker.emqx.io")
EMQX_TRUST_BASE = "https://capillary-sled-monogamy.ngrok-free.dev/"


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
    test_mode = cfg.get("MQTT_PUBLIC_TEST_MODE", False)
    signed_mode = cfg.get("MQTT_PUBLIC_SIGNED_MODE", False)
    if not isinstance(test_mode, bool) or not isinstance(signed_mode, bool) or (test_mode and signed_mode):
        raise ValueError("Select at most one public MQTT mode using JSON booleans")
    if a.image:
        if test_mode:
            raise ValueError("OTA signing is disabled in public test mode")
        command, manifest = release(cfg, a.image.read_bytes(), a.request_id, a.seq,
                                    a.manifest_url, a.image_url)
        a.out.mkdir(parents=True, exist_ok=True)
        for name, value in (("command.json", command), ("manifest.json", manifest)):
            (a.out / name).write_text(json.dumps(value, indent=2), encoding="utf-8")
        print("Created signed command.json and manifest.json; no credentials printed")
        return

    limits = {"device": 31, "ssid": 32, "password": 64, "broker": 191, "trust_base": 191}
    if test_mode:
        cfg.setdefault("logical_device", "ESP32_001")
        limits["logical_device"] = 31
        if not cfg.get("device"):
            cfg["device"] = "SC_TEST_" + secrets.token_hex(8)
        cfg.setdefault("broker", PUBLIC_BROKER)
        if any(cfg.get(field) for field in ("mqtt_user", "mqtt_password", "key_hex")):
            raise ValueError("Leave MQTT credentials and key_hex empty in public test mode")
    elif signed_mode:
        cfg.setdefault("device", "ESP32_001")
        cfg.setdefault("broker", PUBLIC_BROKER)
        cfg.setdefault("trust_base", EMQX_TRUST_BASE)
        if any(cfg.get(field) for field in ("mqtt_user", "mqtt_password")):
            raise ValueError("Leave MQTT credentials empty for signed EMQX mode")
    else:
        cfg.setdefault("device", "ESP32_001")
        limits.update(mqtt_user=63, mqtt_password=127)
    for field, limit in limits.items():
        if field not in cfg or not isinstance(cfg[field], str) or len(cfg[field].encode()) > limit:
            raise ValueError(f"Missing or oversized {field}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,31}", cfg["device"]):
        raise ValueError("Invalid device ID")
    if not cfg["ssid"] or not trusted_url(cfg["trust_base"], cfg["trust_base"]):
        raise ValueError("Provision Wi-Fi and an HTTPS trust base")
    if test_mode:
        if (cfg["broker"] != PUBLIC_BROKER or not re.fullmatch(r"SC_TEST_[a-f0-9]{16}", cfg["device"])
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,31}", cfg["logical_device"])):
            raise ValueError("Public test mode requires the EMQX TLS endpoint and a test device alias")
        cfg = {field: cfg[field] for field in limits}
        cfg["MQTT_PUBLIC_TEST_MODE"] = True
    elif signed_mode:
        if cfg["broker"] != PUBLIC_BROKER or cfg["device"] != "ESP32_001":
            raise ValueError("Signed EMQX mode requires the EMQX TLS endpoint and ESP32_001")
        cfg.setdefault("key_hex", secrets.token_hex(32))
        cfg = {field: cfg[field] for field in (*limits, "key_hex")}
        cfg["MQTT_PUBLIC_SIGNED_MODE"] = True
    else:
        broker = urlsplit(cfg["broker"])
        host = (broker.hostname or "").lower().rstrip(".")
        if (broker.scheme != "mqtts" or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
                or any(host == public or host.endswith("." + public) for public in PUBLIC_HOSTS)
                or broker.path or broker.port == 0
                or any(c in cfg["broker"] for c in "\r\n\t @?#\\")
                or not cfg["mqtt_user"] or not cfg["mqtt_password"]):
            raise ValueError("Provision an authenticated private TLS broker")
        cfg.setdefault("key_hex", secrets.token_hex(32))
    if not test_mode:
        if not isinstance(cfg["key_hex"], str) or not re.fullmatch(r"[a-fA-F0-9]{64}", cfg["key_hex"]):
            raise ValueError("key_hex must contain 32 bytes in hex")
        cfg["key_hex"] = cfg["key_hex"].lower()
    text = json.dumps(cfg, separators=(",", ":"), ensure_ascii=True)
    if len(text) >= 4096:
        raise ValueError("Configuration exceeds firmware bound")
    a.out.mkdir(parents=True, exist_ok=True)
    if not test_mode:
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
    if test_mode:
        print("MQTT_PUBLIC_TEST_DEVICE=" + cfg["device"])
    print("Created secret provisioning files. Do not commit, serve or log these files.")


if __name__ == "__main__":
    main()
