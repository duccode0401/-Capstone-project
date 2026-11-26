#Requires -RunAsAdministrator
param(
    [string]$Root = 'C:\SmartChargeMQTT',
    [switch]$AllowWebLan
)
$ErrorActionPreference = 'Stop'
$service = Get-Service -Name mosquitto
$program = 'C:\Program Files (x86)\Mosquitto'
$main = Join-Path $program 'mosquitto.conf'
$config = Join-Path $Root 'config\smartcharge.conf'
$acl = Join-Path $Root 'config\acl'
$ca = Join-Path $Root 'pki\root-ca.pem'
$cert = Join-Path $Root 'pki\server.pem'
$key = Join-Path $Root 'pki\server.key'
$passwords = Join-Path $Root 'passwords.txt'
foreach ($file in @($main,$config,$acl,$ca,$cert,$key,$passwords)) {
    if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing $file" }
}
$openssl = Get-Command openssl.exe -ErrorAction SilentlyContinue
$opensslPath = if ($openssl) { $openssl.Source } else { 'C:\Program Files\Git\usr\bin\openssl.exe' }
foreach ($identity in @('-verify_hostname','-verify_ip')) {
    $address = if ($identity -eq '-verify_hostname') { 'localhost' } else { '127.0.0.1' }
    & $opensslPath verify -CAfile $ca $identity $address $cert
    if ($LASTEXITCODE -ne 0) { throw "Certificate validation failed for $address" }
}
$base = 'iot/charge_station/v1'
$messages = @('telemetry','logs','status','heartbeat','ota/status')
$health = 'smartcharge/local/health/#'
$newAcl = @('user ESP32_001') +
    @($messages | ForEach-Object { "topic write $base/$_" }) +
    @("topic read $base/orders","topic read $base/ota/command","topic read $base/ack/ESP32_001",'','user smartcharge_backend') +
    @($messages | ForEach-Object { "topic read $base/$_" }) +
    @("topic write $base/orders","topic write $base/ota/command","topic write $base/ack/+","topic readwrite $health")
$newConfig = @(
    'allow_anonymous false',
    "global_plugin $program\mosquitto_password_file.dll",
    "plugin_opt_password_file $passwords",
    "global_plugin $program\mosquitto_acl_file.dll",
    "plugin_opt_acl_file $acl",
    'listener 8883 127.0.0.1',
    "certfile $cert",
    "keyfile $key",
    'tls_version tlsv1.2'
)
$backup = Join-Path $Root ('backups\local-web-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Path $backup | Out-Null
Copy-Item -LiteralPath $main,$config,$acl -Destination $backup
Write-Host "Backup $backup"
try {
    Set-Content -LiteralPath $acl -Value $newAcl -Encoding ascii
    Set-Content -LiteralPath $config -Value $newConfig -Encoding ascii
    if ($service.Status -eq 'Running') { Restart-Service -Name mosquitto }
    else { Start-Service -Name mosquitto }
    $service.WaitForStatus('Running',[TimeSpan]::FromSeconds(15))
    $listeners = Get-NetTCPConnection -State Listen -LocalPort 8883 -ErrorAction Stop
    if (!$listeners -or @($listeners | Where-Object LocalAddress -ne '127.0.0.1').Count) {
        throw 'Expected Mosquitto TLS to listen only on 127.0.0.1:8883.'
    }
    Write-Host 'Mosquitto Running, TLS 127.0.0.1:8883.'
} catch {
    Copy-Item -LiteralPath (Join-Path $backup 'smartcharge.conf') -Destination $config -Force
    Copy-Item -LiteralPath (Join-Path $backup 'acl') -Destination $acl -Force
    $service.Refresh()
    if ($service.Status -eq 'Running') { Restart-Service -Name mosquitto }
    else { Start-Service -Name mosquitto }
    throw
}

if ($AllowWebLan) {
    $venv = Join-Path (Split-Path $PSScriptRoot) '.venv\Scripts\python.exe'
    $webProgram = & $venv -c 'import sys; print(sys._base_executable)'
    if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $webProgram)) {
        throw 'Unable to identify the Django Python executable for the firewall rule.'
    }
    $rule = 'SmartCharge-Web-LocalSubnet'
    if (!(Get-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name $rule -DisplayName 'SmartCharge Web LAN (TCP 8000)' `
            -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 `
            -Program $webProgram -RemoteAddress LocalSubnet -Profile Any -EdgeTraversalPolicy Block | Out-Null
    }
    Write-Host 'Windows Firewall allows SmartCharge Web TCP 8000 from the local subnet.'
}
