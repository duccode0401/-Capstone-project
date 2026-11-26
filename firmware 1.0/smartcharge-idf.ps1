param(
    [ValidateSet("build", "monitor", "size", "test")]
    [string]$Action = "build",
    [string]$Port = "COM3"
)
$ErrorActionPreference = "Stop"
$idfProfile = "C:\Espressif\tools\Microsoft.v5.5.1.PowerShell_profile.ps1"
if (-not (Test-Path -LiteralPath $idfProfile)) { throw "ESP-IDF profile not found: $idfProfile" }
. $idfProfile
Push-Location $PSScriptRoot
try {
    if ($Action -eq "build") {
        & idf.py -B build-smartcharge build
    } elseif ($Action -eq "monitor") {
        & idf.py -B build-smartcharge -p $Port monitor
    } elseif ($Action -eq "size") {
        & idf.py -B build-smartcharge size
    } else {
        & python -m unittest discover -s tests -v
    }
    if ($LASTEXITCODE -ne 0) { throw "ESP-IDF command failed: $LASTEXITCODE" }
} finally { Pop-Location }
