# Dot-source this file to load ESP-IDF into the current PowerShell session.
$idfProfile = 'C:\Espressif\tools\Microsoft.v5.5.1.PowerShell_profile.ps1'
if (-not (Test-Path -LiteralPath $idfProfile -PathType Leaf)) {
    throw "ESP-IDF 5.5.1 PowerShell profile not found: $idfProfile"
}
. $idfProfile
if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) {
    throw 'ESP-IDF profile did not make idf.py available.'
}
