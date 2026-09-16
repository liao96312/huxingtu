@echo off
rem One-click launcher. ASCII-only so Windows cmd code pages cannot corrupt it.
setlocal
set "ROOT=%~dp0"
echo [start-dev] Starting backend and frontend...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%start-dev.ps1" %*
if errorlevel 1 (
  echo.
  echo [start-dev] Startup failed. Review the error above.
  pause
)
endlocal
