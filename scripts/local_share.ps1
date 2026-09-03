[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "url")]
    [string]$Action = "start",
    [string]$ProjectId = "",
    [ValidateRange(1, 24)]
    [int]$TtlHours = 24,
    [string]$Proxy = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cloudflared = Join-Path $projectRoot "tools\cloudflared.exe"
$debugRoot = Join-Path $projectRoot "datasets\debug"
$pidFile = Join-Path $debugRoot "cloudflared.pid"
$urlFile = Join-Path $debugRoot "cloudflared-url.txt"
$outputLog = Join-Path $debugRoot "cloudflared.log"
$errorLog = Join-Path $debugRoot "cloudflared-error.log"
$viewerUrl = "http://localhost:5173"
$apiUrl = "http://127.0.0.1:8000"

function Write-Info([string]$message) { Write-Host "[local-share] $message" -ForegroundColor Cyan }
function Write-Ok([string]$message) { Write-Host "[local-share] OK  $message" -ForegroundColor Green }

function Stop-Tunnel {
    if (Test-Path -LiteralPath $pidFile) {
        $savedPid = (Get-Content -LiteralPath $pidFile -TotalCount 1) -as [int]
        $process = if ($savedPid) { Get-Process -Id $savedPid -ErrorAction SilentlyContinue } else { $null }
        if ($process -and $process.Path -eq $cloudflared) {
            Stop-Process -Id $savedPid -Force
            Write-Ok "tunnel stopped; the public URL is no longer reachable"
        }
    }
    Remove-Item -LiteralPath $pidFile, $urlFile -Force -ErrorAction SilentlyContinue
}

function Test-Http([string]$url) {
    try {
        $response = Invoke-WebRequest -Uri $url -Method Get -TimeoutSec 3 -UseBasicParsing
        return $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Start-LocalStack {
    $backendReady = Test-Http "$apiUrl/api/health"
    $viewerReady = Test-Http $viewerUrl
    if ($backendReady -and $viewerReady) { return }
    Write-Info "local services are incomplete; starting them through start-dev.ps1"
    if (-not $backendReady -and -not $viewerReady) {
        & (Join-Path $projectRoot "start-dev.ps1")
    } elseif (-not $backendReady) {
        & (Join-Path $projectRoot "start-dev.ps1") -NoFront
    } else {
        & (Join-Path $projectRoot "start-dev.ps1") -NoBack
    }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if ((Test-Http "$apiUrl/api/health") -and (Test-Http $viewerUrl)) { return }
        Start-Sleep -Milliseconds 700
    }
    throw "local Viewer/API did not become ready; run .\start-dev.ps1"
}

New-Item -ItemType Directory -Force -Path $debugRoot | Out-Null

if ($Action -eq "stop") {
    Stop-Tunnel
    exit 0
}

if ($Action -eq "url") {
    if (-not (Test-Path -LiteralPath $urlFile)) { throw "no local-share tunnel is active" }
    Get-Content -LiteralPath $urlFile -TotalCount 1
    exit 0
}

if (-not (Test-Path -LiteralPath $cloudflared)) {
    throw "missing $cloudflared"
}
if ($ProjectId -and $ProjectId -notmatch "^[a-zA-Z0-9_-]{1,128}$") {
    throw "ProjectId may contain only letters, digits, underscores, and dashes"
}

Start-LocalStack
Stop-Tunnel

if (-not $Proxy -and (Test-NetConnection 127.0.0.1 -Port 7897 -InformationLevel Quiet -WarningAction SilentlyContinue)) {
    $Proxy = "http://127.0.0.1:7897"
}
$previousHttpProxy = $env:HTTP_PROXY
$previousHttpsProxy = $env:HTTPS_PROXY
$previousNoProxy = $env:NO_PROXY
$publicUrl = $null
try {
    foreach ($attempt in 1..3) {
        $attemptProxy = if ($attempt -lt 3) { $Proxy } else { "" }
        $env:HTTP_PROXY = $attemptProxy
        $env:HTTPS_PROXY = $attemptProxy
        $env:NO_PROXY = "localhost,127.0.0.1"
        Write-Info "cloudflared attempt $attempt/3 proxy=$($attemptProxy -ne '')"
        Remove-Item -LiteralPath $outputLog, $errorLog -Force -ErrorAction SilentlyContinue
        $startArguments = @{
            FilePath = $cloudflared
            ArgumentList = @("tunnel", "--url", $viewerUrl, "--no-autoupdate", "--protocol", "http2")
            WorkingDirectory = $projectRoot
            WindowStyle = "Hidden"
            RedirectStandardOutput = $outputLog
            RedirectStandardError = $errorLog
            PassThru = $true
        }
        $process = Start-Process @startArguments
        Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline -and -not $publicUrl) {
            Start-Sleep -Milliseconds 700
            $logs = @($outputLog, $errorLog) | Where-Object { Test-Path -LiteralPath $_ } | ForEach-Object { Get-Content -Raw -LiteralPath $_ }
            $matches = [regex]::Matches(($logs -join [Environment]::NewLine), "https://[a-z0-9-]+\.trycloudflare\.com")
            $publicUrl = $matches.Value | Where-Object { $_ -ne "https://api.trycloudflare.com" } | Select-Object -Last 1
            if ($process.HasExited) { break }
        }
        if ($publicUrl) { break }
        Stop-Tunnel
    }
} finally {
    $env:HTTP_PROXY = $previousHttpProxy
    $env:HTTPS_PROXY = $previousHttpsProxy
    $env:NO_PROXY = $previousNoProxy
}
if (-not $publicUrl) {
    Stop-Tunnel
    throw "no trycloudflare URL received; inspect datasets\debug\cloudflared-error.log"
}

Set-Content -LiteralPath $urlFile -Value $publicUrl -Encoding ascii
Write-Ok "temporary tunnel: $publicUrl"
Write-Host "  Upload: $publicUrl/?pipeline=1"

if ($ProjectId) {
    $body = @{ ttlSeconds = $TtlHours * 3600; retentionMode = "shareOnly" } | ConvertTo-Json -Compress
    try {
        $share = Invoke-RestMethod -Method Post -Uri "$apiUrl/api/v1/projects/$ProjectId/shares" -ContentType "application/json" -Body $body -TimeoutSec 10
        $customerUrl = "$($publicUrl.TrimEnd('/'))$($share.shareUrl)"
        Write-Ok "$TtlHours-hour customer URL: $customerUrl"
    } catch {
        Write-Info "tunnel is active, but project $ProjectId has no pano-manifest yet; use Share in the Viewer after generation"
    }
} else {
    Write-Host "  Open a generated Viewer and click Share to create a 24-hour customer URL."
}
Write-Host "  Stop: powershell -ExecutionPolicy Bypass -File scripts\local_share.ps1 -Action stop"
