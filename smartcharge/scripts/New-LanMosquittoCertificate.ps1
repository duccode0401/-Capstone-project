param(
    [Parameter(Mandatory)][string]$CaFile,
    [Parameter(Mandatory)][string]$CaKey,
    [Parameter(Mandatory)][string]$ServerKey,
    [Parameter(Mandatory)][string]$OutputFile
)
$ErrorActionPreference = 'Stop'
$openssl = Get-Command openssl.exe -ErrorAction SilentlyContinue
$opensslPath = if ($openssl) { $openssl.Source } else { 'C:\Program Files\Git\usr\bin\openssl.exe' }
if (!(Test-Path -LiteralPath $opensslPath -PathType Leaf)) { throw 'OpenSSL executable not found.' }
if (Test-Path -LiteralPath $OutputFile) { throw "Output already exists: $OutputFile" }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('smartcharge-cert-' + [Guid]::NewGuid())
New-Item -ItemType Directory -Path $tmp | Out-Null
$csr = Join-Path $tmp 'server.csr'
$ext = Join-Path $tmp 'extensions.conf'
$cert = Join-Path $tmp 'server.pem'
try {
    @('basicConstraints=critical,CA:FALSE','keyUsage=critical,digitalSignature,keyEncipherment',
      'extendedKeyUsage=serverAuth','subjectAltName=DNS:smartcharge.local,DNS:localhost,IP:127.0.0.1') |
        Set-Content -LiteralPath $ext -Encoding Ascii
    & $opensslPath req -new -key $ServerKey -subj /CN=smartcharge.local -out $csr
    if ($LASTEXITCODE) { throw 'Certificate request failed.' }
    $serial = '0x' + [Guid]::NewGuid().ToString('N')
    & $opensslPath x509 -req -in $csr -CA $CaFile -CAkey $CaKey -set_serial $serial -days 365 -sha256 -extfile $ext -out $cert
    if ($LASTEXITCODE) { throw 'Certificate signing failed.' }
    foreach ($name in @('smartcharge.local','localhost')) {
        & $opensslPath verify -purpose sslserver -CAfile $CaFile -verify_hostname $name $cert
        if ($LASTEXITCODE) { throw "Certificate verification failed for $name." }
    }
    & $opensslPath verify -purpose sslserver -CAfile $CaFile -verify_ip 127.0.0.1 $cert
    if ($LASTEXITCODE) { throw 'Certificate verification failed for 127.0.0.1.' }
    Copy-Item -LiteralPath $cert -Destination $OutputFile
} finally {
    foreach ($file in @($csr,$ext,$cert)) {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
    }
    if (!(Get-ChildItem -LiteralPath $tmp -Force)) { Remove-Item -LiteralPath $tmp }
}
