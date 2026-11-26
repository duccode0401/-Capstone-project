#Requires -RunAsAdministrator
param(
    [string]$Root = 'C:\SmartChargeMQTT',
    [switch]$AllowWebLan,
    [switch]$AllowMqttLan
)
$ErrorActionPreference = 'Stop'
$service = Get-Service -Name mosquitto
$program = 'C:\Program Files (x86)\Mosquitto'
$main = Join-Path $program 'mosquitto.conf'
$config = Join-Path $Root 'config\smartcharge.conf'
$acl = Join-Path $Root 'config\acl'
$ca = Join-Path $Root 'pki\root-ca.pem'
$certName = if ($AllowMqttLan) { 'pki\server-lan.pem' } else { 'pki\server.pem' }
$cert = Join-Path $Root $certName
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
if ($AllowMqttLan) {
    & $opensslPath verify -purpose sslserver -CAfile $ca -verify_hostname smartcharge.local $cert
    if ($LASTEXITCODE -ne 0) { throw 'LAN certificate must be valid for smartcharge.local.' }
    $subject = & $opensslPath x509 -in $cert -noout -subject -nameopt RFC2253
    $san = & $opensslPath x509 -in $cert -noout -ext subjectAltName
    if ($LASTEXITCODE -ne 0 -or $subject -notmatch '^subject=CN=smartcharge\.local(?:,|$)' -or
        ($san -join ' ') -notmatch 'DNS:smartcharge\.local(?:,|$)') {
        throw 'LAN certificate requires CN and DNS SAN smartcharge.local.'
    }
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
    ('listener 8883 ' + $(if ($AllowMqttLan) { '0.0.0.0' } else { '127.0.0.1' })),
    "certfile $cert",
    "keyfile $key",
    'tls_version tlsv1.2'
)
function Set-LanFirewallRules([string]$Name, [string]$Protocol, [int]$Port, [string]$Program) {
    $allow = "$Name-LocalSubnet"
    $block = "$Name-Block-Untrusted"
    if (Get-NetFirewallRule -Name $allow -ErrorAction SilentlyContinue) {
        Set-NetFirewallRule -Name $allow -Profile Private -RemoteAddress LocalSubnet -Enabled True
    } else {
        New-NetFirewallRule -Name $allow -DisplayName "$Name trusted LAN only" -Program $Program -Direction Inbound -Action Allow -Protocol $Protocol -LocalPort $Port -RemoteAddress LocalSubnet -Profile Private -EdgeTraversalPolicy Block | Out-Null
    }
    if (!(Get-NetFirewallRule -Name $block -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name $block -DisplayName "$Name block untrusted networks" -Program $Program -Direction Inbound -Action Block -Protocol $Protocol -LocalPort $Port -Profile Public,Domain -EdgeTraversalPolicy Block | Out-Null
    }
}

if ($AllowMqttLan -or $AllowWebLan) {
    $profiles = Get-NetFirewallProfile -PolicyStore ActiveStore
    if (@($profiles | Where-Object { $_.Enabled -ne 'True' -or $_.DefaultInboundAction -eq 'Allow' }).Count) {
        throw 'LAN mode requires Windows Firewall enabled and inbound deny by default.'
    }
    $venv = Join-Path (Split-Path $PSScriptRoot) '.venv\Scripts\python.exe'
    $pythonProgram = & $venv -c 'import sys; print(sys._base_executable)'
    if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $pythonProgram)) {
        throw 'Unable to find the base Python executable for LAN firewall rules.'
    }
    if ($AllowWebLan) { Set-LanFirewallRules 'SmartCharge-Web' TCP 8000 $pythonProgram }
    if ($AllowMqttLan) {
        Set-LanFirewallRules 'SmartCharge-MQTT' TCP 8883 (Join-Path $program 'mosquitto.exe')
        Set-LanFirewallRules 'SmartCharge-mDNS' UDP 5353 $pythonProgram
    }
}

$backup = Join-Path $Root ('backups\local-web-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
New-Item -ItemType Directory -Path $backup | Out-Null
Copy-Item -LiteralPath $main,$config,$acl -Destination $backup
Write-Host "Backup $backup"
try {
    if (!$AllowMqttLan) { Set-Content -LiteralPath $acl -Value $newAcl -Encoding ascii }
    Set-Content -LiteralPath $config -Value $newConfig -Encoding ascii
    if ($service.Status -eq 'Running') { Restart-Service -Name mosquitto }
    else { Start-Service -Name mosquitto }
    $service.WaitForStatus('Running',[TimeSpan]::FromSeconds(15))
    $listeners = Get-NetTCPConnection -State Listen -LocalPort 8883 -ErrorAction Stop
    $expected = if ($AllowMqttLan) { '0.0.0.0' } else { '127.0.0.1' }
    if (!$listeners -or @($listeners | Where-Object LocalAddress -ne $expected).Count) {
        throw ("Expected Mosquitto TLS listener at {0}:8883." -f $expected)
    }
    Write-Host ("Mosquitto Running, TLS {0}:8883." -f $expected)
} catch {
    Copy-Item -LiteralPath (Join-Path $backup 'smartcharge.conf') -Destination $config -Force
    Copy-Item -LiteralPath (Join-Path $backup 'acl') -Destination $acl -Force
    $service.Refresh()
    if ($service.Status -eq 'Running') { Restart-Service -Name mosquitto }
    else { Start-Service -Name mosquitto }
    throw
}
