"""Fault-sequence MODEL tests, not execution of the ESP-IDF bootloader.

Based on inspected IDF 5.5.1 selection logic. Power-cut hardware validation is
still mandatory; this model cannot prove flash-sector atomicity or GPIO behavior.
"""
import unittest


def boot(selection, image_valid=True):
    if selection in ("factory", "aborted") or not image_valid:
        return "factory"
    if selection == "pending_verify":
        return "factory"  # bootloader marks unconfirmed candidate ABORTED
    return "ota_0"


class BootModelTests(unittest.TestCase):
    def test_cuts_during_download_always_keep_factory_selected(self):
        for percent in (0, 10, 50, 90, 100):
            with self.subTest(percent=percent):
                self.assertEqual(boot("factory", image_valid=percent == 100), "factory")

    def test_candidate_only_after_verification_commit(self):
        self.assertEqual(boot("new", image_valid=True), "ota_0")
        self.assertEqual(boot("new", image_valid=False), "factory")

    def test_unconfirmed_reboot_returns_recovery(self):
        self.assertEqual(boot("pending_verify"), "factory")

    def test_valid_firmware_crashes_do_not_automatically_rollback(self):
        self.assertEqual(boot("valid"), "ota_0")


if __name__ == "__main__":
    unittest.main()
