"""Read-only checks against actual Django canonical expressions (no Django/DB imports).

No settings/.env, database, network publish or Web writes are used. Tests skip
when the adjacent Django source is not part of the local checkout.
"""
import ast
import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ota_protocol import command_text, manifest_text, sign


class WebContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = ROOT.parent / "smartcharge/myapp/ota_services.py"
        if not source.exists():
            raise unittest.SkipTest("Adjacent Django source unavailable; read-only contract comparison skipped")
        cls.tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        cls.functions = {n.name: n for n in cls.tree.body if isinstance(n, ast.FunctionDef)}

    def setUp(self):
        self.key = "12" * 32  # TEST ONLY; never read a production device key.
        self.base = "https://firmware.example.test"
        self.device = SimpleNamespace(device_id="ESP32_001")
        self.firmware = SimpleNamespace(version="1.1.0", file_size=1024, sha256="ab" * 32)
        self.job = SimpleNamespace(device=self.device, firmware=self.firmware,
                                   request_id="ota-test-only", sequence=42)

    def test_actual_web_command_canonical_matches_firmware_reference(self):
        # Backend may factor command construction into prepare_ota; compare the
        # actual SC-CMD-1 expression instead of coupling the test to a helper name.
        node = next(n for fn in self.functions.values() for n in ast.walk(fn)
                    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "canonical" for t in n.targets)
                    and any(isinstance(v, ast.Constant) and isinstance(v.value,str) and "SC-CMD-1" in v.value
                            for v in ast.walk(n.value)))
        url = self.base + "/firmware/manifest/ota-test-only/test-only-capability/"
        canonical = eval(compile(ast.Expression(node.value), "web-command-canonical", "eval"),
                         dict(device=self.device, firmware=self.firmware, job=self.job, manifest_url=url))
        command = dict(device=self.device.device_id, request_id=self.job.request_id,
                       version=self.firmware.version, manifest_url=url, seq="42")
        self.assertEqual(canonical, command_text(command))
        self.assertFalse(canonical.endswith("\n"))

    def test_actual_web_manifest_schema_and_signature_match_firmware(self):
        namespace = dict(hashlib=hashlib, hmac=hmac, _https_base_url=lambda: self.base,
                         _device_key=lambda device: bytes.fromhex(self.key))
        for name in ("_signature", "build_manifest"):
            code = ast.fix_missing_locations(ast.Module(body=[self.functions[name]], type_ignores=[]))
            exec(compile(code, "web-manifest-reference", "exec"), namespace)
        manifest = namespace["build_manifest"](self.job, "test-only-capability")
        self.assertEqual(set(manifest), {"device", "request_id", "version", "url", "size", "sha256", "chip", "seq", "signature"})
        self.assertEqual(manifest["seq"], "42")
        self.assertEqual(manifest["chip"], "esp32")
        self.assertEqual(manifest["signature"], sign(manifest_text(manifest), self.key))

    def test_web_command_topic_unchanged(self):
        assignment = next(n for n in self.tree.body if isinstance(n, ast.Assign) and
                          any(isinstance(t, ast.Name) and t.id == "OTA_COMMAND_TOPIC" for t in n.targets))
        self.assertEqual(ast.literal_eval(assignment.value), "iot/charge_station/v1/ota/command")


if __name__ == "__main__":
    unittest.main()
