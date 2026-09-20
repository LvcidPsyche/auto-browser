param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('assistant_one', 'assistant_two', 'assistant_three')]
    [string]$AgentName
)

$ErrorActionPreference = 'Stop'
$keyPath = Join-Path $env:USERPROFILE '.ssh\stone_main'
if (-not (Test-Path -LiteralPath $keyPath)) {
    throw "SSH key is missing: $keyPath"
}

$tokenLine = & ssh.exe -i $keyPath -o BatchMode=yes -o StrictHostKeyChecking=yes root@204.168.150.160 "grep '^BROKER_AGENT_TOKENS=' /opt/auto-browser/.env"
if ($LASTEXITCODE -ne 0 -or -not $tokenLine) {
    throw 'Could not retrieve the protected agent credentials.'
}

$entries = (($tokenLine -split '=', 2)[1] -split ',')
$matching = @($entries | Where-Object { $_.StartsWith("${AgentName}:") })
if ($matching.Count -ne 1) {
    throw 'The selected agent credential was not found.'
}

$token = ($matching[0] -split ':', 2)[1]
if (-not $token) {
    throw 'The selected agent credential was empty.'
}
Set-Clipboard -Value $token
Remove-Variable token, tokenLine, entries, matching
Write-Output "Credential for $AgentName copied to clipboard. Give it only to that agent, then clear the clipboard."
