@echo off
REM start-dev.bat — 一键启动「户型图→360°全景」本地开发环境（双击即可）
REM 转发到 start-dev.ps1（PowerShell 脚本做了依赖检查、后台启动、健康检查）
setlocal
set "ROOT=%~dp0"
echo [start-dev] 正在启动全套开发环境...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%start-dev.ps1" %*
if errorlevel 1 (
  echo.
  echo [start-dev] 启动过程有错误，请查看上方信息。
  pause
)
endlocal
