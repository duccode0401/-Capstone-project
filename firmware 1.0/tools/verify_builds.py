"""Validate two real IDF build artifacts and their flashing destinations."""
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
    expected = [("nvs", 1, 2, 0x9000, 0x4000), ("otadata", 1, 0, 0xD000, 0x2000),
                ("phy_init", 1, 1, 0xF000, 0x1000), ("factory", 0, 0, 0x10000, 0x100000),
                ("ota_0", 0, 16, 0x110000, 0x2F0000)]
    partitions = []
    for directory, name, subtype, limit, offset in (
        (ROOT / "build-smartcharge", "smartcharge", "ota_0", 0x2F0000, 0x110000),
        (ROOT / "recovery/build-recovery", "recovery", "factory", 0x100000, 0x10000),
    ):
        table_path = directory / "partition_table/partition-table.bin"
        binary = directory / f"{name}.bin"
        table = PartitionTable.from_binary(table_path.read_bytes())
        actual = [(p.name, p.type, p.subtype, p.offset, p.size) for p in table]
        if actual != expected:
            raise ValueError(f"Unexpected partition table in {directory}")
        table.verify()
        for entry in table:
            alignment = 0x10000 if entry.type == 0 else 0x1000
            if entry.offset % alignment or entry.offset + entry.size > 0x400000:
                raise ValueError("Partition alignment/flash overflow")
        partitions.append(table_path.read_bytes())
        config = json.loads((directory / "config/sdkconfig.json").read_text())
        if config.get("IDF_TARGET") != "esp32" or config.get("ESPTOOLPY_FLASHSIZE") != "4MB":
            raise ValueError("Both applications must target ESP32 / 4MB")
        if not config.get("BOOTLOADER_APP_ROLLBACK_ENABLE") or config.get("BOOTLOADER_APP_ANTI_ROLLBACK"):
            raise ValueError("Require rollback; eFuse anti-rollback is incompatible with this factory layout")
        args = json.loads((directory / "flasher_args.json").read_text())
        if int(args["app"]["offset"], 0) != offset:
            raise ValueError(f"Unsafe {name} flash destination")
        subprocess.run([sys.executable, str(idf / "components/partition_table/check_sizes.py"),
            "partition", "--type", "app", "--subtype", subtype, str(table_path), str(binary)], check=True)
        subprocess.run([sys.executable, str(ROOT / "tools/check_artifact.py"), str(binary), hex(limit)], check=True)
        subprocess.run([sys.executable, "-m", "esptool", "--chip", "esp32", "image_info", str(binary)], check=True)
        desc = json.loads((directory / "project_description.json").read_text())
        if desc.get("git_revision") != "v5.5.1":
            raise ValueError(f"Expected tested IDF v5.5.1, found {desc.get('git_revision')}")
        if name == "recovery":
            commands = json.loads((directory / "compile_commands.json").read_text())
            core = [c for c in commands if Path(c["file"]).name == "sc_core.c"]
            if len(core) != 1 or "-DSC_RECOVERY=1" not in core[0]["command"]:
                raise ValueError("Recovery must compile its own core with SC_RECOVERY=1")
            nm = str(Path(desc["c_compiler"]).with_name("xtensa-esp32-elf-nm.exe" if os.name == "nt" else "xtensa-esp32-elf-nm"))
            symbols = subprocess.check_output([nm, str(directory / "recovery.elf")], text=True)
            forbidden = ("power_meter_init", "power_meter_get", "adc_continuous_start", "lv_init",
                         "lv_timer_handler", "handle_payment", "update_energy")
            if any(line.split()[-1] in forbidden for line in symbols.splitlines() if line.split()):
                raise ValueError("Recovery unexpectedly links measurement, LVGL or charging logic")
            print("Recovery symbol audit: no LVGL / ADC continuous / payment / charge integration")
        print(f"Verified {name}: IDF {desc['git_revision']}, ESP32, 4MB, offset={offset:#x}")
    if partitions[0] != partitions[1]:
        raise ValueError("Partition binaries differ")
    print("Both partition binaries are identical; all bounds and offsets verified")


if __name__ == "__main__":
    verify()
