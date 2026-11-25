# firmware_smartcharge

Firmware component copied from ADC/main for SmartCharge project.

Contains example display, wifi/mqtt and power meter code for ESP32 (ESP-IDF).

## Build, flash and monitor (Windows, ESP-IDF 5.5.1)

Open a new PowerShell terminal and load the ESP-IDF environment installed on
this machine. Do this again whenever a new terminal is opened:

```powershell
. "C:\Espressif\tools\Microsoft.v5.5.1.PowerShell_profile.ps1"
cd D:\-Capstone-project-main\firmware
```

Do not use `D:\Espressif\v5.5.1\esp-idf\export.ps1` for this installation.
The profile above sets the correct `IDF_PATH`, `IDF_TOOLS_PATH` and ESP-IDF
Python environment under `C:\Espressif\tools`.

Build the firmware:

```powershell
idf.py -B build-esp32 build
```

Flash the firmware through COM3:

```powershell
idf.py -B build-esp32 -p COM3 flash
```

Open the COM3 serial monitor at 115200 baud:

```powershell
idf.py -B build-esp32 -p COM3 -b 115200 monitor
```

Press `Ctrl + ]` to exit the serial monitor.

Build, flash and open the serial monitor in one command:

```powershell
idf.py -B build-esp32 -p COM3 -b 115200 build flash monitor
```

External components are downloaded automatically into `managed_components`:
ILI9341 2.1.0, LVGL (version resolved in `dependencies.lock`), and the
ILI9341 dependency `espressif/cmake_utilities`. ADC, SPI, GPIO, LCD, timers,
Wi-Fi, MQTT, NVS and FreeRTOS are included with ESP-IDF.
`sdkconfig.defaults` selects ESP32 and a 1500 KiB application partition because
the firmware exceeds the default 1 MiB application partition.
Continuous ADC interrupts use `CONFIG_ADC_CONTINUOUS_ISR_IRAM_SAFE=y` so they
can run while flash operations disable the cache. NVS is initialized before
starting the ADC to avoid a cache-access panic during startup.

Configure Wi-Fi using `idf.py -B build-esp32 menuconfig` under
`Example Configuration`. If Windows assigns another serial port, replace
`COM3` in the commands above with that port.
