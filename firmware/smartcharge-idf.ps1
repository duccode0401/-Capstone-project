param(
    [ValidateSet("build", "flash", "monitor", "flash-monitor", "recovery-build")]
    [string]$Action = "build",
    [string]$Port = "COM3"
)
$ErrorActionPreference = "Stop"
$idfProfile = "C:\Espressif\tools\Microsoft.v5.5.1.PowerShell_profile.ps1"
if (-not (Test-Path -LiteralPath $idfProfile)) { throw "ESP-IDF profile not found: $idfProfile" }
. $idfProfile
Push-Location $PSScriptRoot
try {
    if ($Action -eq "recovery-build") {
        Push-Location (Join-Path $PSScriptRoot "recovery")
        try { & idf.py -B build-recovery -D SDKCONFIG=sdkconfig.recovery build }
        finally { Pop-Location }
    } elseif ($Action -eq "build") {
        & idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota build
    } elseif ($Action -eq "monitor") {
        & idf.py -B build-smartcharge -D SDKCONFIG=sdkconfig.ota -p $Port monitor
    } else {
        throw "Use the explicit initial-flash procedure in OTA_MQTT_PROTOCOL.md. Generic flash is disabled to protect Recovery and provisioning."
    }
    if ($LASTEXITCODE -ne 0) { throw "ESP-IDF command failed: $LASTEXITCODE" }
} finally { Pop-Location }
