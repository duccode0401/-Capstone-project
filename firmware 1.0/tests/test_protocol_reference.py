"""Pure-Python contract tests that run even when no native host compiler exists."""
import copy
import hmac
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from protocol import order_text, sign


class ProtocolReferenceTests(unittest.TestCase):
    KEY = "42" * 32

    def setUp(self):
        self.order = {
            "device": "ESP32_001",
            "command": "START_CHARGE",
            "port": 1,
            "order_id": "session-123",
            "amount_vnd": 10000,
            "expires_at": 1_800_000_060,
            "seq": 5,
        }

    def test_canonical_order_contract_is_stable(self):
        self.assertEqual(order_text(self.order),
            "SC-ORDER-1\nESP32_001\nSTART_CHARGE\n1\nsession-123\n10000\n1800000060\n5")
        self.assertEqual(len(sign(order_text(self.order), self.KEY)), 64)

    def test_mutation_invalidates_signature(self):
        signature = sign(order_text(self.order), self.KEY)
        for field, value in (("device", "ESP32_002"), ("port", 2),
                             ("amount_vnd", 20000), ("expires_at", 1_800_000_061),
                             ("seq", 6), ("order_id", "session-124")):
            changed = copy.copy(self.order)
            changed[field] = value
            self.assertFalse(hmac.compare_digest(signature,
                sign(order_text(changed), self.KEY)), field)

    def test_key_is_part_of_authority(self):
        text = order_text(self.order)
        self.assertFalse(hmac.compare_digest(sign(text, self.KEY), sign(text, "24" * 32)))


if __name__ == "__main__":
    unittest.main()
