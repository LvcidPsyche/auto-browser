param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'

$keyPath = Join-Path $env:USERPROFILE '.ssh\stone_main'
if (-not (Test-Path -LiteralPath $keyPath)) {
    throw "SSH key is missing: $keyPath"
}

$sshArgs = @(
    '-N', '-i', $keyPath,
    '-o', 'BatchMode=yes',
    '-o', 'StrictHostKeyChecking=yes',
    '-o', 'ExitOnForwardFailure=yes',
    '-L', '18000:127.0.0.1:18000',
    '-L', '18001:127.0.0.1:18001',
    '-L', '16080:127.0.0.1:16080',
    'root@204.168.150.160'
)

$tunnel = Start-Process -FilePath 'ssh.exe' -ArgumentList $sshArgs -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 2
if ($tunnel.HasExited) {
    throw 'The private SSH tunnel did not start. Check SSH access and local ports.'
}

if ($CheckOnly) {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:18001/owner' -UseBasicParsing -TimeoutSec 10
        if ($response.StatusCode -ne 200) {
            throw 'The owner page did not respond through the private tunnel.'
        }
        Write-Output 'Private owner tunnel and page are working.'
    } finally {
        Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue
    }
    return
}

$tokenLine = & ssh.exe -i $keyPath -o BatchMode=yes -o StrictHostKeyChecking=yes root@204.168.150.160 "grep '^BROKER_OWNER_TOKEN=' /opt/auto-browser/.env"
if ($LASTEXITCODE -ne 0 -or -not $tokenLine) {
    Stop-Process -Id $tunnel.Id
    throw 'Could not retrieve the selected token from the private server.'
}
$token = ($tokenLine -split '=', 2)[1]
if (-not $token) {
    Stop-Process -Id $tunnel.Id
    throw 'The selected token was empty.'
}
Set-Clipboard -Value $token
Remove-Variable token, tokenLine

Start-Process 'http://127.0.0.1:18001/owner'
Write-Output "Private tunnel is active (process $($tunnel.Id)). The owner token is on your clipboard. Paste it into the owner page, then clear the clipboard."
