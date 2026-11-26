"""Explicit UART single-app migration installer; never invoked by build/tests.

Always creates a 4 MiB backup first. Existing NVS is preserved unless --nvs is supplied.
Does not run as part of build, and never writes eFuses.
"""
import argparse
from pathlib import Path
import subprocess
import sys
from verify_builds import ROOT, verify


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--nvs", type=Path)
    p.add_argument("--backup", required=True, type=Path)
    a = p.parse_args()
    if a.nvs and a.nvs.stat().st_size != 0x4000:
        raise SystemExit("NVS must be exactly 16 KiB")
    if a.backup.exists():
        raise SystemExit("Backup path must not already exist")
    verify()
    tool = [sys.executable, "-m", "esptool", "--chip", "esp32", "--port", a.port]
    info = subprocess.run(tool + ["flash_id"], capture_output=True, text=True, check=True)
    if "Detected flash size: 4MB" not in info.stdout:
        raise SystemExit("Refusing migration: esptool did not detect a 4MB physical flash")
    a.backup.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(tool + ["read_flash", "0", "0x400000", str(a.backup)], check=True)
    if a.backup.stat().st_size != 0x400000:
        raise SystemExit("Flash backup is incomplete")
    build = ROOT / "build-smartcharge"
    images = ["0x1000", str(build / "bootloader/bootloader.bin"),
              "0x8000", str(build / "partition_table/partition-table.bin"),
              "0x10000", str(build / "smartcharge.bin")]
    if a.nvs:
        images[4:4] = ["0x9000", str(a.nvs)]
    subprocess.run(tool + ["write_flash", "--flash_mode", "dio", "--flash_freq", "40m",
                           "--flash_size", "4MB"] + images, check=True)


if __name__ == "__main__":
    main()
