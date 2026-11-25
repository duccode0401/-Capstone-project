"""Exercise real provisioning generator and release signer with TEST credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ota_protocol import command_text, manifest_text, sign


class ToolTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("IDF_PATH") and (ROOT / "build-smartcharge/smartcharge.bin").exists(),
                         "Requires ESP-IDF environment and real SmartCharge build")
    def test_provision_and_sign_real_application(self):
        with tempfile.TemporaryDirectory(prefix="sc-provision-test-") as directory:
            d = Path(directory)
            config = dict(device="ESP32_TEST", ssid="test-network", password="test-only-password",
                          broker="mqtts://mqtt.example.test:8883", mqtt_user="test-device",
                          mqtt_password="test-only-password", trust_base="https://firmware.example.test/fw/")
            source = d / "config.json"
            source.write_text(json.dumps(config))
            tool = [sys.executable, str(ROOT / "tools/provision.py")]
            result = subprocess.run(tool + ["--config", str(source), "--out", str(d / "out"), "--bootstrap"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((d / "out/nvs.bin").stat().st_size, 0x4000)
            secret = json.loads((d / "out/device-secret.json").read_text())
            self.assertEqual(len(bytes.fromhex(secret["key_hex"])), 32)
            result = subprocess.run(tool + ["--config", str(d / "out/device-secret.json"),
                "--out", str(d / "release"), "--image", str(ROOT / "build-smartcharge/smartcharge.bin"),
                "--request-id", "test-1", "--seq", "1", "--manifest-url", config["trust_base"] + "manifest/test-1",
                "--image-url", config["trust_base"] + "test.bin"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            command = json.loads((d / "release/command.json").read_text())
            manifest = json.loads((d / "release/manifest.json").read_text())
            self.assertEqual(command["signature"], sign(command_text(command), secret["key_hex"]))
            self.assertEqual(manifest["signature"], sign(manifest_text(manifest), secret["key_hex"]))


if __name__ == "__main__":
    unittest.main()
