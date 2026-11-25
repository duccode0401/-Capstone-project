# SmartCharge firmware (ESP-IDF 5.5.1 / ESP32 4 MiB)

Main application: ADC continuous power measurement, LVGL/ILI9341, payment,
relay control, Wi-Fi/MQTT, 10-second telemetry and bounded event logging.
Recovery is a separate application under `../recovery/`.

Read [OTA_MQTT_PROTOCOL.md](../OTA_MQTT_PROTOCOL.md) before flashing. The layout
has changed: factory is now **Recovery (1 MiB)** and SmartCharge runs from
**ota_0 (0x110000, 0x2F0000 bytes)**. Do not flash this app to factory or reuse
the old build-esp32 flash commands. Initial migration needs NVS reprovisioning
and both binaries; the documented installer backs up flash first.

From the firmware directory in PowerShell:

```powershell
. "C:\Espressif\tools\Microsoft.v5.5.1.PowerShell_profile.ps1"
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota build
Push-Location recovery
idf.py -B build-recovery -D SDKCONFIG=sdkconfig.recovery build
Pop-Location
python tools/verify_builds.py
python -m unittest discover -s tests -v
```

After the explicit initial migration, open the serial monitor:

```powershell
idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -p COM3 monitor
```

Press `Ctrl + ]` to exit. Replace COM3 with the actual port.

External components download into managed_components: ILI9341 2.1.0,
LVGL (resolved version in dependencies.lock), and cmake_utilities.
NVS initializes before continuous ADC starts, and ADC interrupts are IRAM-safe.
Credentials and per-device OTA trust settings live in the shared NVS namespace
`sc`; use the provisioning tool documented in OTA_MQTT_PROTOCOL.md.
Provision Wi-Fi through NVS. For bench telemetry without an MQTT account, follow
[the EMQX public test guide](../PUBLIC_MQTT_TEST.md).

MEM logs show physical flash, partition bounds, free/minimum/largest internal
heap, task stack high watermarks and message drops. Runtime diagnostics run
approximately every 10 seconds. Build and host-test results, plus outstanding
hardware tests, are recorded in [VALIDATION_RESULTS.md](../VALIDATION_RESULTS.md).
