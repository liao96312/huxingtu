# start-dev.ps1 - one-shot launcher for the floorplan -> 360 panorama dev stack
#
# Starts:
#   1. Backend FastAPI  (uvicorn -> http://127.0.0.1:8000)
#   2. Frontend Vite     (npm run dev -> http://localhost:5173, /api proxied to 8000)
#   3. Checks deps + Real-ESRGAN tool, prints URLs + healthcheck
#
# Usage (PowerShell):
#   .\start-dev.ps1             start backend + frontend
#   .\start-dev.ps1 -NoFront    backend only
#   .\start-dev.ps1 -NoBack     frontend only
#   .\start-dev.ps1 -Restart    kill old listeners on 8000/5173 first

param(
    [switch]$NoFront,
    [switch]$NoBack,
    [switch]$Restart,
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$Root        = $PSScriptRoot
$BackendPort = 8000
$FrontPort   = 5173
$PythonExe   = Join-Path $Root ".venv\Scripts\python.exe"

function Info($msg)  { Write-Host "[start-dev] $msg" -ForegroundColor Cyan }
function Ok($msg)    { Write-Host "[start-dev] OK  $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "[start-dev] WARN $msg" -ForegroundColor Yellow }

function Stop-Port($port) {
    $found = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($found) {
        $pids = $found | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $pids) {
            Info "port $port held by PID $procId, stopping"
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
}
if ($Restart) { Stop-Port $BackendPort; Stop-Port $FrontPort }

# ---- deps ----
if (-not (Test-Path $PythonExe)) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExe) { throw "python not found (create .venv or install Python 3.12+)" }
if (-not (Get-Command npm -ErrorAction SilentlyContinue))    { throw "npm not found (install Node 18+)" }
if (-not (Test-Path (Join-Path $Root "tools\realesrgan\package\realesrgan-ncnn-vulkan.exe"))) {
    Warn "Real-ESRGAN tool missing; run scripts\install_realesrgan.ps1. AI panorama will skip super-resolution."
}
if (-not (Test-Path (Join-Path $Root "apps\pano-viewer\node_modules"))) {
    Info "frontend deps missing, running npm install..."
    Push-Location (Join-Path $Root "apps\pano-viewer")
    & npm.cmd install
    Pop-Location
}

# ---- backend ----
$BackendLog = Join-Path $Root "datasets\debug\pano-api.log"
New-Item -ItemType Directory -Force -Path (Split-Path $BackendLog) | Out-Null
if (-not $NoBack) {
    $backendListener = Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($backendListener) {
        Ok "backend already listening on port $BackendPort; reusing PID=$($backendListener.OwningProcess)"
    } else {
        Info "starting backend uvicorn on port $BackendPort (PID will be written to pid file)"
        $names = @("TUZI_API_KEY","TUZI_BASE_URL","TUZI_VISION_PRIMARY_MODEL","TUZI_VISION_REVIEW_MODEL")
        foreach ($name in $names) {
            $val = [Environment]::GetEnvironmentVariable($name,"User")
            if ($val -and -not [Environment]::GetEnvironmentVariable($name,"Process")) {
                Set-Item -Path "Env:$name" -Value $val
            }
        }
        $env:PYTHONPATH = "$Root\backend;$env:PYTHONPATH"
        $backend = Start-Process -FilePath $PythonExe -ArgumentList @(
            "-m","uvicorn","pano_platform.main:app","--app-dir","$Root\backend",
            "--host","127.0.0.1","--port","$BackendPort"
        ) -WorkingDirectory $Root -WindowStyle Hidden -PassThru
        Info "backend launcher PID=$($backend.Id)"
    }
} else {
    Warn "backend skipped (--NoBack)"
}

# ---- frontend ----
$FrontLog = Join-Path $Root "datasets\debug\pano-viewer.log"
if (-not $NoFront) {
    $frontListener = Get-NetTCPConnection -LocalPort $FrontPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($frontListener) {
        Ok "frontend already listening on port $FrontPort; reusing PID=$($frontListener.OwningProcess)"
    } else {
        Info "starting frontend Vite on port $FrontPort"
        $npmCmd = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
        if (-not $npmCmd) { $npmCmd = "npm.cmd" }
        Start-Process -FilePath $npmCmd -ArgumentList @("run","dev","--","--host","127.0.0.1") -WorkingDirectory (Join-Path $Root "apps\pano-viewer") -WindowStyle Hidden | Out-Null
        $frontDeadline = (Get-Date).AddSeconds(15)
        do {
            $frontListener = Get-NetTCPConnection -LocalPort $FrontPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $frontListener) { Start-Sleep -Milliseconds 500 }
        } while (-not $frontListener -and (Get-Date) -lt $frontDeadline)
        if (-not $frontListener) { throw "frontend did not listen on 127.0.0.1:$FrontPort" }
        Ok "frontend PID=$($frontListener.OwningProcess) listening on 127.0.0.1:$FrontPort"
    }
    $frontListener.OwningProcess | Out-File (Join-Path $Root "datasets\debug\pano-viewer.pid")
} else {
    Warn "frontend skipped (--NoFront)"
}

# ---- healthcheck ----
$deadline = (Get-Date).AddSeconds(30)
if (-not $NoBack) {
    Info "waiting for backend healthcheck..."
    $r = $null
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/api/health" -TimeoutSec 3
            if ($r.status -eq "ok") {
                $backendListener = Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue |
                    Where-Object LocalAddress -in @("127.0.0.1", "0.0.0.0") |
                    Select-Object -First 1
                if ($backendListener) {
                    $backendListener.OwningProcess | Out-File (Join-Path $Root "datasets\debug\pano-api.pid")
                }
                Ok "backend healthy: $($r.status) v$($r.version) PID=$($backendListener.OwningProcess)"
                break
            }
        } catch { Start-Sleep -Milliseconds 800 }
    }
    if (-not $r) { Warn "backend healthcheck not confirmed, see log: $BackendLog" }
}

Write-Host ""
Write-Host "==================== STARTED ====================" -ForegroundColor Magenta
Write-Host "  Panorama viewer : http://127.0.0.1:$FrontPort/"
Write-Host "  Upload pipeline: http://127.0.0.1:$FrontPort/?pipeline=1"
Write-Host "  Backend API    : http://127.0.0.1:$BackendPort/api/health"
Write-Host "  API docs       : http://127.0.0.1:$BackendPort/docs"
Write-Host "=================================================" -ForegroundColor Magenta
if (-not $NoFront -and -not $NoOpen) {
    Start-Process "http://127.0.0.1:$FrontPort/?pipeline=1"
}
