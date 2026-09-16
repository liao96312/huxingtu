$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root "tools\realesrgan\package"
$archive = Join-Path $root "tools\realesrgan\realesrgan-ncnn-vulkan-20220424-windows.zip"
$url = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"

New-Item -ItemType Directory -Path (Split-Path -Parent $archive) -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $target "realesrgan-ncnn-vulkan.exe"))) {
    Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
    Expand-Archive -LiteralPath $archive -DestinationPath $target -Force
}

$tool = Join-Path $target "realesrgan-ncnn-vulkan.exe"
if (-not (Test-Path -LiteralPath $tool)) { throw "Real-ESRGAN install did not produce $tool" }
Write-Output "Real-ESRGAN ready: $tool"
