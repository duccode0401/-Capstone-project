"""Validate the single SmartCharge IDF build, 4 MB layout, and flash destination."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def verify():
    idf = Path(os.environ["IDF_PATH"])
    sys.path.insert(0, str(idf / "components/partition_table"))
    from gen_esp32part import PartitionTable

    directory = ROOT / "build-smartcharge"
    table_path = directory / "partition_table/partition-table.bin"
    binary = directory / "smartcharge.bin"
    expected = [("nvs", 1, 2, 0x9000, 0x4000),
                ("phy_init", 1, 1, 0xF000, 0x1000),
                ("factory", 0, 0, 0x10000, 0x3F0000)]
    table = PartitionTable.from_binary(table_path.read_bytes())
    actual = [(p.name, p.type, p.subtype, p.offset, p.size) for p in table]
    if actual != expected:
        raise ValueError(f"Unexpected single-app partition table: {actual}")
    table.verify()
    for entry in table:
        alignment = 0x10000 if entry.type == 0 else 0x1000
        if entry.offset % alignment or entry.offset + entry.size > 0x400000:
            raise ValueError("Partition alignment or 4 MB flash overflow")
    config = json.loads((directory / "config/sdkconfig.json").read_text())
    if config.get("IDF_TARGET") != "esp32" or config.get("ESPTOOLPY_FLASHSIZE") != "4MB":
        raise ValueError("Build must target ESP32 with 4 MB flash")
    args = json.loads((directory / "flasher_args.json").read_text())
    if int(args["app"]["offset"], 0) != 0x10000:
        raise ValueError("Application must flash at factory offset 0x10000")
    subprocess.run([sys.executable, str(idf / "components/partition_table/check_sizes.py"),
        "partition", "--type", "app", "--subtype", "factory", str(table_path), str(binary)], check=True)
    subprocess.run([sys.executable, str(ROOT / "tools/check_artifact.py"),
                    str(binary), hex(0x3F0000)], check=True)
    subprocess.run([sys.executable, "-m", "esptool", "--chip", "esp32", "image_info", str(binary)], check=True)
    desc = json.loads((directory / "project_description.json").read_text())
    if desc.get("git_revision") != "v5.5.1":
        raise ValueError(f"Expected ESP-IDF v5.5.1, found {desc.get('git_revision')}")
    print(f"Verified SmartCharge {desc['project_version']}: ESP-IDF {desc['git_revision']}, "
          f"ESP32, 4 MB, factory offset=0x10000, size={binary.stat().st_size}")


if __name__ == "__main__":
    verify()
