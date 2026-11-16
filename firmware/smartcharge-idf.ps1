param(
    [ValidateSet("build", "flash", "monitor", "flash-monitor")]
    [string]$Action = "build",
    [string]$Port = "COM3"
)

$ErrorActionPreference = "Stop"
$idfProfile = "C:\Espressif\tools\Microsoft.v5.5.1.PowerShell_profile.ps1"
$buildPath = Join-Path $PSScriptRoot "build-esp32"

if (-not (Test-Path -LiteralPath $idfProfile)) {
    throw "ESP-IDF profile not found: $idfProfile"
}
if (-not (Test-Path -LiteralPath (Join-Path $buildPath "build.ninja"))) {
    throw "Build directory not found. Configure the ESP-IDF project first: $buildPath"
}

. $idfProfile
$env:ESPPORT = $Port
$env:ESPBAUD = "460800"

function Invoke-IdfBuildTarget([string]$targetName) {
    & cmake --build $buildPath --target $targetName
    if ($LASTEXITCODE -ne 0) {
        throw "CMake target '$targetName' failed with exit code $LASTEXITCODE"
    }
}

switch ($Action) {
    "build" {
        & cmake --build $buildPath
        if ($LASTEXITCODE -ne 0) { throw "Build failed with exit code $LASTEXITCODE" }
    }
    "flash" { Invoke-IdfBuildTarget "flash" }
    "monitor" { Invoke-IdfBuildTarget "monitor" }
    "flash-monitor" {
        Invoke-IdfBuildTarget "flash"
        Invoke-IdfBuildTarget "monitor"
    }
}
