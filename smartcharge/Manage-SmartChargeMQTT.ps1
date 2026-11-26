[CmdletBinding()]
param([ValidateSet('Install','Backup','Check','Restore')][string]$Action='Check', [string]$BackupPath)
$ErrorActionPreference = 'Stop'
$root = 'C:\SmartChargeMQTT'
$install = 'C:\Program Files (x86)\Mosquitto'
$broker = "$install\mosquitto.exe"
$entry = "$install\mosquitto.conf"
$dir = "$root\config"
$pki = "$root\pki"
$ca = "$pki\root-ca.pem"
$issuer = "$pki\issuer"
$openssl = 'C:\Program Files\Git\mingw64\bin\openssl.exe'
$rule = 'SmartCharge-MQTT-TLS-LAN'
$ip = '192.168.1.104'

function Run([string]$Program, [string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'
    & $Program @Arguments 2>&1 | Out-Host
    if ($LASTEXITCODE) { throw "$Program exited with $LASTEXITCODE" }
}

function Backup {
    $path = Join-Path "$root\backups" (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    Copy-Item -LiteralPath $entry -Destination "$path\service.conf"
    if (Test-Path -LiteralPath $dir) { Copy-Item -LiteralPath $dir -Destination "$path\config" -Recurse }
    if (Get-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue) {
        New-Item -ItemType File -Path "$path\firewall-present" | Out-Null
    }
    $path
}

function Enable-LanFirewall {
    $lan = Get-NetIPAddress -IPAddress $ip -AddressFamily IPv4
    if ($lan.PrefixLength -ne 24) { throw 'Expected LAN 192.168.1.0/24.' }
    Enable-LanFirewall
}

function Restore([string]$Path) {
    $source = (Resolve-Path -LiteralPath $Path).Path
    if (!(Test-Path -LiteralPath "$source\service.conf" -PathType Leaf)) { throw 'Missing service.conf in backup.' }
    Stop-Service mosquitto
    try {
        if (Test-Path -LiteralPath "$source\config") {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
            Copy-Item -Path "$source\config\*" -Destination $dir -Force
        } else {
            Remove-Item -LiteralPath "$dir\smartcharge.conf","$dir\acl" -ErrorAction SilentlyContinue
        }
        Copy-Item -LiteralPath "$source\service.conf" -Destination $entry -Force
        if (Test-Path -LiteralPath "$source\firewall-present") {
            Enable-LanFirewall
        } elseif (Get-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue) {
            Remove-NetFirewallRule -Name $rule
        }
    } finally { Start-Service mosquitto }
}

function Check {
    Run $broker @('-c',$entry,'--test-config')
    $service = Get-CimInstance Win32_Service -Filter "Name='mosquitto'"
    if ($service.State -ne 'Running') { throw 'Mosquitto service is not running.' }
    $listeners = @(Get-NetTCPConnection -State Listen -OwningProcess $service.ProcessId)
    if ($listeners.Count -ne 2 -or @($listeners | Where-Object {
        $_.LocalPort -ne 8883 -or $_.LocalAddress -notin @($ip,'127.0.0.1')
    }).Count) { throw 'Unexpected Mosquitto listeners.' }
    $listeners | Select-Object LocalAddress,LocalPort,OwningProcess | Out-Host
    Get-NetFirewallRule -Name $rule | Get-NetFirewallAddressFilter | Select-Object LocalAddress,RemoteAddress | Out-Host
    Run $openssl @('verify','-CAfile',$ca,'-verify_ip',$ip,"$pki\server.pem")
    Run $openssl @('verify','-CAfile',$ca,'-verify_hostname','localhost',"$pki\server.pem")
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell session.'
}
if ($Action -eq 'Check') { Check; return }
if ($Action -eq 'Backup') { Backup; return }
if ($Action -eq 'Restore') {
    if (!$BackupPath) { throw 'Provide -BackupPath.' }
    Write-Host "Before restore, saved $(Backup)"
    Restore $BackupPath
    return
}

$passwordFile = "$root\passwords.txt"
$hash = (Get-FileHash -LiteralPath $passwordFile).Hash
$lan = Get-NetIPAddress -IPAddress $ip -AddressFamily IPv4
if ($lan.PrefixLength -ne 24) { throw 'Expected LAN 192.168.1.0/24.' }
$backup = Backup
Write-Host "Backup $backup"
$admins = @('*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F')
foreach ($path in @($root,$dir,$pki,$issuer,"$root\backups")) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    $access = $admins
    if ($path -notin @($issuer,"$root\backups")) { $access += '*S-1-5-32-545:(OI)(CI)RX' }
    Run icacls.exe (@($path,'/inheritance:r','/grant:r') + $access)
}
foreach ($path in @($passwordFile,"$pki\server.key")) {
    if (!(Test-Path -LiteralPath $path)) { New-Item -ItemType File -Path $path | Out-Null }
    Run icacls.exe @($path,'/inheritance:r','/grant:r','*S-1-5-18:F','*S-1-5-32-544:F')
}

if (!(Test-Path -LiteralPath "$issuer\root-ca.key") -and !(Test-Path -LiteralPath $ca)) {
    Run $openssl @('req','-x509','-newkey','rsa:3072','-noenc','-sha256','-days','3650',
        '-subj','/CN=SmartCharge Root CA','-addext','basicConstraints=critical,CA:TRUE,pathlen:0',
        '-addext','keyUsage=critical,keyCertSign,cRLSign','-keyout',"$issuer\root-ca.key",'-out',$ca)
}
if (!(Test-Path -LiteralPath "$pki\server.pem")) {
    Run $openssl @('req','-new','-newkey','rsa:2048','-noenc','-sha256','-subj',"/CN=$ip",
        '-keyout',"$pki\server.key",'-out',"$issuer\server.csr")
    @'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=IP:192.168.1.104,DNS:localhost,IP:127.0.0.1
'@ | Set-Content -LiteralPath "$issuer\server.ext" -Encoding Ascii
    Run $openssl @('x509','-req','-in',"$issuer\server.csr",'-CA',$ca,'-CAkey',"$issuer\root-ca.key",
        '-CAserial',"$issuer\root-ca.srl",'-CAcreateserial','-days','397','-sha256',
        '-extfile',"$issuer\server.ext",'-out',"$pki\server.pem")
}

Stop-Service mosquitto
try {
    @'
user ESP32_001
topic write iot/charge_station/v1/telemetry
topic write iot/charge_station/v1/logs
topic write iot/charge_station/v1/status
topic write iot/charge_station/v1/ota/status
topic read iot/charge_station/v1/orders
topic read iot/charge_station/v1/ota/command

user smartcharge_backend
topic read iot/charge_station/v1/telemetry
topic read iot/charge_station/v1/logs
topic read iot/charge_station/v1/status
topic read iot/charge_station/v1/ota/status
topic write iot/charge_station/v1/orders
topic write iot/charge_station/v1/ota/command
'@ | Set-Content -LiteralPath "$dir\acl" -Encoding Ascii
    @'
allow_anonymous false
global_plugin C:/Program Files (x86)/Mosquitto/mosquitto_password_file.dll
plugin_opt_password_file C:/SmartChargeMQTT/passwords.txt
global_plugin C:/Program Files (x86)/Mosquitto/mosquitto_acl_file.dll
plugin_opt_acl_file C:/SmartChargeMQTT/config/acl

listener 8883 192.168.1.104
certfile C:/SmartChargeMQTT/pki/server.pem
keyfile C:/SmartChargeMQTT/pki/server.key
tls_version tlsv1.2

listener 8883 127.0.0.1
certfile C:/SmartChargeMQTT/pki/server.pem
keyfile C:/SmartChargeMQTT/pki/server.key
tls_version tlsv1.2
'@ | Set-Content -LiteralPath "$dir\smartcharge.conf" -Encoding Ascii
    'include_dir C:/SmartChargeMQTT/config' | Set-Content -LiteralPath $entry -Encoding Ascii
    Run $broker @('-c',$entry,'--test-config')
    if (!(Get-NetFirewallRule -Name $rule -ErrorAction SilentlyContinue)) {
        $allow = @{
            Name=$rule; DisplayName='SmartCharge MQTT TLS (LAN)'; Direction='Inbound'
            Action='Allow'; Protocol='TCP'; LocalPort=8883; LocalAddress=$ip
            RemoteAddress='192.168.1.0/24'; InterfaceAlias=$lan.InterfaceAlias; Profile='Any'
            Program=$broker; EdgeTraversalPolicy='Block'
        }
        New-NetFirewallRule @allow | Out-Null
    }
    Start-Service mosquitto
    Check
    if ((Get-FileHash -LiteralPath $passwordFile).Hash -ne $hash) { throw 'Password file changed.' }
} catch {
    Write-Warning "Restoring broker configuration from $backup"
    Restore $backup
    throw
}
