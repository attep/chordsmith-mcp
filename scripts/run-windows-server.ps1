<#
.SYNOPSIS
Starts the ChordSmith MCP server natively on Windows (streamable HTTP), wired to the local
VST3 and SFZ folders so the vst/sfizz engines and list_plugins work outside Docker.

.DESCRIPTION
Sets the CHORDSMITH_* environment variables for a checkout (defaults assume this script lives
in <checkout>\scripts) and starts the project venv's `chordsmith-mcp` console script. No
secrets live here: authentication is off by default (local machine only) — pass -Password with
-Auth password to enable OAuth, which also needs CHORDSMITH_PUBLIC_URL for non-local clients.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\run-windows-server.ps1 -Port 18001

.EXAMPLE
scripts\run-windows-server.ps1 -VstDir "D:\ChordSmith\bin\ik-vst3"
#>
param(
    [int]$Port = 18001,
    [string]$ListenHost = "127.0.0.1",
    [ValidateSet("off", "password")][string]$Auth = "off",
    [string]$Password = "",
    [string]$OutputDir = "",
    [string]$StateDir = "",
    [string]$SfzDir = "",
    [string]$SfizzRender = "",
    [string]$VstHost = "",
    [string]$VstDir = "",
    [string]$Ffmpeg = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutputDir)    { $OutputDir = Join-Path $root "data" }
if (-not $StateDir)     { $StateDir = Join-Path $root "state-local" }
if (-not $SfzDir)       { $SfzDir = Join-Path $root "sfz" }
if (-not $SfizzRender)  { $SfizzRender = Join-Path $root "bin\sfizz\sfizz_render.exe" }
if (-not $VstHost)      { $VstHost = Join-Path $root "bin\vst_host.exe" }
if (-not $VstDir)       { $VstDir = "C:\Program Files\Common Files\VST3" }
if (-not $Ffmpeg)       { $Ffmpeg = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\ffmpeg.exe" }

$server = Join-Path $root ".venv\Scripts\chordsmith-mcp.exe"
if ($Python) {
    $command = $Python
    $arguments = @("-m", "chordsmith.server", "--transport", "streamable-http", "--port", "$Port")
} elseif (Test-Path -LiteralPath $server) {
    $command = $server
    $arguments = @("--transport", "streamable-http", "--port", "$Port")
} else {
    throw "No server found: create .venv (pip install -e .) or pass -Python <python.exe>."
}

New-Item -ItemType Directory -Path $OutputDir, $StateDir -Force | Out-Null

$env:CHORDSMITH_TRANSPORT = "streamable-http"
$env:CHORDSMITH_HOST = $ListenHost
$env:CHORDSMITH_PORT = "$Port"
$env:CHORDSMITH_OUTPUT_DIR = $OutputDir
$env:CHORDSMITH_STATE_DIR = $StateDir
if (Test-Path -LiteralPath $SfzDir)       { $env:CHORDSMITH_SFZ_DIR = $SfzDir }
if (Test-Path -LiteralPath $SfizzRender)  { $env:CHORDSMITH_SFIZZ_RENDER = $SfizzRender }
if (Test-Path -LiteralPath $VstHost)      { $env:CHORDSMITH_VST_HOST = $VstHost }
if (Test-Path -LiteralPath $VstDir)       { $env:CHORDSMITH_VST_DIR = $VstDir }
if (Test-Path -LiteralPath $Ffmpeg)       { $env:CHORDSMITH_FFMPEG = $Ffmpeg }
if ($Auth -eq "off") {
    $env:CHORDSMITH_AUTH = "off"
} else {
    if (-not $Password) { throw "-Auth password needs -Password (or use -Auth off)." }
    $env:CHORDSMITH_AUTH = "password"
    $env:CHORDSMITH_AUTH_PASSWORD = $Password
}

$process = Start-Process -FilePath $command -ArgumentList $arguments -NoNewWindow -PassThru
$url = "http://$ListenHost`:$Port"
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod -Uri "$url/healthz" -TimeoutSec 2
        $version = $health.version
        Write-Host "ChordSmith $version on $url (PID $($process.Id), MCP path $url/mcp)"
        exit 0
    } catch {
        if ($process.HasExited) { throw "Server exited with code $($process.ExitCode) before becoming healthy." }
    }
}
throw "Server did not answer $url/healthz within 30 seconds."
