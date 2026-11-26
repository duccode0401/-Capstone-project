"""Host tests for provisioning output and the single-application layout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ToolTests(unittest.TestCase):
    def test_partition_csv_preserves_nvs_and_phy(self):
        rows = [line.replace(" ", "").lower() for line in
                (ROOT / "partitions.csv").read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(rows, ["nvs,data,nvs,0x9000,0x4000",
                                "phy_init,data,phy,0xf000,0x1000",
                                "factory,app,factory,0x10000,0x3f0000"])
        self.assertFalse(any("ota" in row for row in rows))

    @unittest.skipUnless(os.environ.get("IDF_PATH"), "Requires ESP-IDF NVS generator")
    def test_provisioning_generates_local_secrets_without_printing_them(self):
        with tempfile.TemporaryDirectory(prefix="sc-provision-") as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({"device": "ESP32_TEST", "ssid": "test-network",
                                          "password": "wifi-test-only"}))
            output = root / "out"
            result = subprocess.run([sys.executable, str(ROOT / "tools/provision.py"),
                "--config", str(config), "--out", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((output / "nvs.bin").stat().st_size, 0x4000)
            secret = json.loads((output / "device-secret.json").read_text())
            self.assertEqual(secret["broker"], "mqtts://broker.emqx.io:8883")
            self.assertEqual(len(bytes.fromhex(secret["key_hex"])), 32)
            self.assertGreaterEqual(len(secret["setup_password"]), 8)
            terminal = result.stdout + result.stderr
            self.assertNotIn(secret["key_hex"], terminal)
            self.assertNotIn(secret["setup_password"], terminal)
            self.assertNotIn(secret["password"], terminal)

    def test_ota_and_recovery_sources_removed(self):
        self.assertFalse((ROOT / "recovery/CMakeLists.txt").exists())
        sources = "\n".join(path.read_text(errors="ignore") for path in
                            (ROOT / "components/sc_core").glob("*.c"))
        self.assertNotIn("esp_ota_", sources)
        self.assertNotIn("START_OTA", sources)


if __name__ == "__main__":
    unittest.main()
