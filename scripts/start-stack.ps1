# Start (or repair) the whole local ChordSmith stack after a reboot.
#
# A Cloudflare quick tunnel gets a new hostname every time it starts, and the server's
# CHORDSMITH_PUBLIC_URL (OAuth metadata and token audience) must match it. This script:
#   1. waits for Docker,
#   2. makes sure the shared network and .env exist (migrating hand-started containers),
#   3. starts the tunnel, reads its current URL and writes it to .env,
#   4. (re)starts the server, VOICEVOX and the manager UI,
#   5. mints a manager token when the URL changed (or no token file exists),
#   6. prints exactly what to paste into the UI.
#
# Double-click scripts\start-stack.cmd; run this file directly for the same without the pause.

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$envPath = Join-Path (Get-Location) '.env'

function Write-Step([string]$text) { Write-Host "==> $text" }

function Write-EnvFile([System.Collections.Generic.List[string]]$lines) {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($envPath, [string[]]$lines, $utf8)
}

# 1. Docker engine -------------------------------------------------------------
Write-Step 'waiting for Docker'
$deadline = (Get-Date).AddMinutes(5)
while ($true) {
    docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { break }
    if ((Get-Date) -gt $deadline) { throw 'Docker did not start within 5 minutes. Start Docker Desktop first.' }
    Start-Sleep -Seconds 2
}

# 2. Shared network and .env ---------------------------------------------------
Write-Step 'preparing the network and .env'
docker network inspect chordsmith-tunnel 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { docker network create chordsmith-tunnel | Out-Null }

$have = @{}
if (Test-Path $envPath) {
    Get-Content $envPath | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=') { $have[$matches[1]] = $true }
    }
}
$missing = @(@('CHORDSMITH_AUTH_PASSWORD', 'CHORDSMITH_PUBLIC_URL') | Where-Object { -not $have.ContainsKey($_) })
if ($missing.Count -gt 0) {
    $legacy = @(docker ps -a --format '{{.Names}}' | Where-Object { $_ -eq 'chordsmith' }).Count -gt 0
    if (-not $legacy) {
        throw ".env is missing $($missing -join ', ') and there is no 'chordsmith' container to copy them from."
    }
    $entries = docker inspect chordsmith --format '{{json .Config.Env}}' | ConvertFrom-Json
    $skip = @(
        'PATH', 'LANG', 'GPG_KEY', 'PYTHON_VERSION', 'PYTHON_SHA256', 'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED',
        'CHORDSMITH_HOST', 'CHORDSMITH_PORT', 'CHORDSMITH_OUTPUT_DIR', 'CHORDSMITH_STATE_DIR', 'CHORDSMITH_SFZ_DIR',
        'CHORDSMITH_VST_DIR', 'CHORDSMITH_VST_TEST_DIR', 'CHORDSMITH_SOUNDFONT', 'CHORDSMITH_DIFFSINGER_VOICE',
        'CHORDSMITH_VOICEVOX_URL'
    )
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path $envPath) { Get-Content $envPath | ForEach-Object { $lines.Add($_) } }
    foreach ($entry in $entries) {
        $split = $entry -split '=', 2
        $key = $split[0]
        $value = if ($split.Count -gt 1) { $split[1] } else { '' }
        if ($skip -contains $key -or $have.ContainsKey($key)) { continue }
        $lines.Add("$key=$value")
    }
    Write-EnvFile $lines
    Write-Step '.env created from the existing container'
}

# 3. Migrate hand-started containers to this compose project -------------------
foreach ($name in @('chordsmith', 'chordsmith-cloudflared', 'voicevox')) {
    if (@(docker ps -a --format '{{.Names}}' | Where-Object { $_ -eq $name }).Count -eq 0) { continue }
    $labels = docker inspect $name --format '{{json .Config.Labels}}' 2>$null
    if ($labels -notmatch 'com\.docker\.compose\.project":"chordsmith-stack') {
        docker rm -f $name | Out-Null
        Write-Step "removed the old hand-started container '$name' (now compose-managed)"
    }
}

# 4. Start the tunnel and follow its current hostname --------------------------
Write-Step 'starting the Cloudflare quick tunnel'
docker compose -f docker-compose.stack.yml up -d cloudflared | Out-Null

$url = $null
$deadline = (Get-Date).AddSeconds(90)
while (-not $url -and (Get-Date) -lt $deadline) {
    # cloudflared logs the URL on stderr; cmd /c merges so PowerShell sees plain output
    $raw = cmd /c 'docker logs chordsmith-cloudflared 2>&1'
    $line = $raw | Select-String -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' | Select-Object -Last 1
    if ($line) {
        $url = [regex]::Match($line.ToString(), 'https://[a-z0-9-]+\.trycloudflare\.com').Value
    } else {
        Start-Sleep -Seconds 2
    }
}
if (-not $url) { throw 'No quick-tunnel URL appeared in the cloudflared logs.' }

$lines = New-Object System.Collections.Generic.List[string]
Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*CHORDSMITH_PUBLIC_URL=') { $lines.Add("CHORDSMITH_PUBLIC_URL=$url") } else { $lines.Add($_) }
}
if (-not ($lines | Where-Object { $_ -match '^\s*CHORDSMITH_PUBLIC_URL=' })) {
    $lines.Add("CHORDSMITH_PUBLIC_URL=$url")
}
Write-EnvFile $lines
Write-Step "tunnel URL: $url"

# 5. Server, VOICEVOX and the manager UI ---------------------------------------
Write-Step 'starting the server and VOICEVOX'
docker compose -f docker-compose.stack.yml up -d | Out-Null
Write-Step 'starting the manager UI'
docker compose -f docker-compose.dev.yml up -d | Out-Null

Write-Step 'waiting for /healthz'
$deadline = (Get-Date).AddSeconds(90)
while ($true) {
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:18000/healthz' -TimeoutSec 3 | Out-Null
        break
    } catch {
        if ((Get-Date) -gt $deadline) { throw 'The server did not answer http://127.0.0.1:18000/healthz.' }
        Start-Sleep -Seconds 2
    }
}

# 6. Manager token (only when the URL changed or no token file exists) ---------
$tokenPath = Join-Path (Get-Location) 'monitor-token.ui.json'
$mint = $true
if (Test-Path $tokenPath) {
    $token = Get-Content $tokenPath -Raw | ConvertFrom-Json
    if ($token.server_url -eq $url) { $mint = $false }
}
$minted = $false
if ($mint) {
    Write-Step 'minting a manager token for this URL'
    & (Join-Path (Get-Location) '.venv\Scripts\python.exe') 'scripts\mint_monitor_token.py' `
        '--server' $url '--container' 'chordsmith' '--out' 'monitor-token.ui.json'
    $minted = ($LASTEXITCODE -eq 0)
    if (-not $minted) { Write-Host 'WARNING: token minting failed; see the message above.' }
}

# 7. Summary -------------------------------------------------------------------
Write-Host ''
Write-Host 'ChordSmith is up:'
Write-Host "  server      http://127.0.0.1:18000   (public: $url)"
Write-Host '  manager UI  http://localhost:5173'
Write-Host '  voicevox    running'
Write-Host '  containers  chordsmith, chordsmith-cloudflared, voicevox, chordsmith-frontend-1'
Write-Host ''
if ($minted) {
    Write-Host 'The tunnel URL changed, so the manager needs the new token:'
    Write-Host "  1. open $tokenPath"
    Write-Host '  2. in the UI: Settings -> paste server_url, client ID and refresh token'
    Write-Host '     (leave the Bearer token empty), then Save & reconnect.'
} else {
    Write-Host 'Tunnel URL unchanged - the existing manager token still works.'
}
Write-Host ''
Write-Host 'Windows VST bridge (for plugins): double-click scripts\start-bridge.cmd'
Write-Host 'It reads CHORDSMITH_VST_BRIDGE_TOKEN from .env.'
