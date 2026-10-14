@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "output\best.pth" (
  echo 还没有 output\best.pth，训练完成后再运行。
  pause
  exit /b 1
)
where gh >nul 2>&1
if errorlevel 1 goto manual
gh release upload learned-seg-kit-v2 "output\best.pth" --repo liao96312/huxingtu --clobber
if errorlevel 1 goto manual
echo 已上传到 GitHub，告诉我一声就行。
pause
exit /b 0
:manual
echo 没有安装 gh 或上传失败。已为你打开 output 文件夹，
echo 请把 best.pth（约 50MB）通过微信/QQ/网盘发给我。
explorer output
pause
