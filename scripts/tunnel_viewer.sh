#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-start}"
shift || true
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ROOT/scripts/local_share.ps1" -Action "$ACTION" "$@"
