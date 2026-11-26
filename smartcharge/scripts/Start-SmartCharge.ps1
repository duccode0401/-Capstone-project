param([switch]$LoopbackOnly)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) { throw "Python environment missing: $python" }

$mutex = [Threading.Mutex]::new($false, 'Local\SmartChargeWebLauncher')
if (!$mutex.WaitOne(0)) { throw 'Another SmartCharge launcher is starting.' }
$web = $null
$subscriber = $null
$ready = $false
Push-Location $root
try {
    $service = Get-Service -Name mosquitto
    if ($service.Status -ne 'Running') {
        try { Start-Service -Name mosquitto }
        catch { throw 'Mosquitto is stopped. Start the service from an Administrator window.' }
        $service.WaitForStatus('Running', [TimeSpan]::FromSeconds(15))
    }
    Write-Host 'Mosquitto Windows Service: Running'
    & $python manage.py mqtt_health
    if ($LASTEXITCODE -ne 0) { throw 'MQTT TLS health check failed. Check the local credentials, CA and broker configuration.' }
    & $python manage.py check
    if ($LASTEXITCODE -ne 0) { throw 'Django system check failed.' }
    & $python manage.py migrate --check
    if ($LASTEXITCODE -ne 0) { throw 'Database migrations are pending. Run manage.py migrate.' }
    & $python manage.py collectstatic --noinput --verbosity 0
    if ($LASTEXITCODE -ne 0) { throw 'Static file collection failed.' }

    if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) {
        throw 'Port 8000 is already listening. Stop the existing Web process before starting another.'
    }
    $runningSubscriber = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.ExecutablePath -eq $python -and $_.CommandLine -match 'mqtt_subscriber\.py(?:\s|$)' }
    if ($runningSubscriber) { throw 'The SmartCharge MQTT subscriber is already running.' }

    $run = Join-Path $root '.run'
    New-Item -ItemType Directory -Force $run | Out-Null
    $env:DJANGO_LAN_MODE = if ($LoopbackOnly) { 'false' } else { 'true' }
    $bind = if ($LoopbackOnly) { '127.0.0.1:8000' } else { '0.0.0.0:8000' }
    $subscriber = Start-Process -FilePath $python -ArgumentList @('-u','mqtt_subscriber.py') -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput "$run\mqtt.stdout.log" -RedirectStandardError "$run\mqtt.stderr.log"
    $web = Start-Process -FilePath $python -ArgumentList @('manage.py','runserver',$bind,'--noreload') -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput "$run\web.stdout.log" -RedirectStandardError "$run\web.stderr.log"
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $web.Refresh()
        $subscriber.Refresh()
        if ($web.HasExited -or $subscriber.HasExited) { break }
        $mqttLog = Join-Path $run 'mqtt.stderr.log'
        if ((Test-Path $mqttLog) -and (Select-String -Quiet -LiteralPath $mqttLog -SimpleMatch '[MQTT] SUBSCRIBED')) {
            try {
                $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/static/admin/css/base.css' -UseBasicParsing -TimeoutSec 2
                if ($response.StatusCode -eq 200) { $ready = $true; break }
            } catch {}
        }
        Start-Sleep -Milliseconds 350
    }
    if (!$ready) { throw "Startup did not reach readiness. Read $run\web.stderr.log and $run\mqtt.stderr.log." }

    Write-Host 'Web localhost: http://127.0.0.1:8000/'
    if (!$LoopbackOnly) {
        & $python -c "from myapp.lan import lan_addresses; print('\n'.join('Web LAN: http://'+ip+':8000/' for ip in lan_addresses()))"
    }
    Write-Host "Django PID $($web.Id); MQTT subscriber PID $($subscriber.Id). Logs: $run"
} finally {
    if (!$ready) {
        foreach ($process in @($web,$subscriber)) {
            if ($process -and !$process.HasExited) {
                Get-CimInstance Win32_Process -Filter "ParentProcessId=$($process.Id)" |
                    ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
                Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
            }
        }
    }
    Pop-Location
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
