@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title huxingtu training

echo ============================================
echo   户型图识别模型 - 一键训练
echo   可以随时关掉窗口，再次双击会接着训练
echo ============================================

set "PYEXE="
for %%V in (3.11 3.10 3.12) do (
  if not defined PYEXE (
    py -%%V -c "import sys" >nul 2>&1 && set "PYEXE=py -%%V"
  )
)
if not defined PYEXE (
  if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
)
if not defined PYEXE (
  echo 没找到 Python 3.10/3.11，正在自动下载安装 Python 3.11（约 25MB）...
  curl -L --fail -o python-installer.exe https://registry.npmmirror.com/-/binary/python/3.11.9/python-3.11.9-amd64.exe
  if errorlevel 1 curl -L --fail -o python-installer.exe https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
  if not exist python-installer.exe (
    echo 下载 Python 失败，请检查网络后重新双击。
    pause
    exit /b 1
  )
  python-installer.exe /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 TargetDir="%LOCALAPPDATA%\Programs\Python\Python311"
  del python-installer.exe
  set "PYEXE="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
)
%PYEXE% -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo Python 安装失败，请截图发给我。
  pause
  exit /b 1
)

echo 使用 Python: %PYEXE%
%PYEXE% setup_and_train.py
echo.
echo 窗口可以关闭了。如果上面有报错，请截图发给我。
pause
