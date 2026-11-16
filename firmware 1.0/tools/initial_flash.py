"""Explicit UART migration installer. Invoking this script authorizes a full flash write.

Always creates a 4MiB backup first. Requires pre-generated local provisioning NVS.
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
    p.add_argument("--nvs", required=True, type=Path)
    p.add_argument("--backup", required=True, type=Path)
    a = p.parse_args()
    if a.nvs.stat().st_size != 0x4000 or a.backup.exists():
        raise SystemExit("NVS must be exactly 16KiB; backup path must not already exist")
    verify()
    tool = [sys.executable, "-m", "esptool", "--chip", "esp32", "--port", a.port]
    info = subprocess.run(tool + ["flash_id"], capture_output=True, text=True, check=True)
    if "Detected flash size: 4MB" not in info.stdout:
        raise SystemExit("Refusing migration: esptool did not detect a 4MB physical flash")
    a.backup.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(tool + ["read_flash", "0", "0x400000", str(a.backup)], check=True)
    if a.backup.stat().st_size != 0x400000:
        raise SystemExit("Flash backup is incomplete")
    recovery = ROOT / "recovery/build-recovery"
    subprocess.run(tool + ["write_flash", "--flash_mode", "dio", "--flash_freq", "40m", "--flash_size", "4MB",
        "0x1000", str(recovery / "bootloader/bootloader.bin"),
        "0x8000", str(recovery / "partition_table/partition-table.bin"),
        "0x9000", str(a.nvs), "0xd000", str(recovery / "ota_data_initial.bin"),
        "0x10000", str(recovery / "recovery.bin"),
        "0x110000", str(ROOT / "build-smartcharge/smartcharge.bin")], check=True)


if __name__ == "__main__":
    main()
