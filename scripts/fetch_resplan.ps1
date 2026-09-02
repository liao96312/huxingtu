param(
    [string]$Proxy = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$registryPath = Join-Path $projectRoot "datasets\external\registry.json"
$record = (Get-Content -Raw -LiteralPath $registryPath | ConvertFrom-Json).datasets |
    Where-Object { $_.id -eq "resplan" }
$destination = Join-Path $projectRoot "tmp\external\resplan"
New-Item -ItemType Directory -Force -Path $destination | Out-Null

if (-not $Proxy) {
    $localProxy = Test-NetConnection -ComputerName 127.0.0.1 -Port 7897 -InformationLevel Quiet -WarningAction SilentlyContinue
    if ($localProxy) {
        $Proxy = "http://127.0.0.1:7897"
    }
}

function Receive-VerifiedFile {
    param(
        [string]$Url,
        [string]$Target,
        [long]$ExpectedBytes,
        [string]$ExpectedSha256
    )

    if (Test-Path -LiteralPath $Target) {
        $existing = Get-Item -LiteralPath $Target
        $existingHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Target).Hash
        if ($existing.Length -eq $ExpectedBytes -and $existingHash -eq $ExpectedSha256) {
            Write-Host "verified existing $Target"
            return
        }
        throw "existing file failed verification: $Target"
    }

    $partial = "$Target.part"
    $arguments = @(
        "--fail", "--location", "--continue-at", "-",
        "--retry", "10", "--retry-all-errors", "--retry-delay", "2",
        "--connect-timeout", "30", "--max-time", "1800",
        "--ssl-revoke-best-effort"
    )
    if ($Proxy) {
        $arguments += @("--proxy", $Proxy)
    }
    $arguments += @("--output", $partial, $Url)
    & curl.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "download failed with curl exit code $LASTEXITCODE"
    }

    $download = Get-Item -LiteralPath $partial
    $downloadHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $partial).Hash
    if ($download.Length -ne $ExpectedBytes -or $downloadHash -ne $ExpectedSha256) {
        throw "download verification failed: $partial"
    }
    Move-Item -LiteralPath $partial -Destination $Target
    Write-Host "downloaded and verified $Target"
}

Receive-VerifiedFile `
    -Url $record.artifactUrl `
    -Target (Join-Path $destination "ResPlan.zip") `
    -ExpectedBytes $record.artifactBytes `
    -ExpectedSha256 $record.artifactSha256

Receive-VerifiedFile `
    -Url $record.splitUrl `
    -Target (Join-Path $destination "split.json") `
    -ExpectedBytes $record.splitBytes `
    -ExpectedSha256 $record.splitSha256
