#!/usr/bin/env bash
# Fetch CubiCasa5K code + pretrained weights (CC BY-NC 4.0, non-commercial only)
# into tmp/models/cubicasa5k (git-ignored). Then: pip install torch (CPU is fine).
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=tmp/models/cubicasa5k
mkdir -p tmp/models
[ -d "$DEST/.git" ] || git clone --depth 1 https://github.com/CubiCasa/CubiCasa5k "$DEST"
if [ ! -f "$DEST/model_best_val_loss_var.pkl" ]; then
  pip install -q gdown
  gdown 1gRB7ez1e4H7a9Y09lLqRuna0luZO5VRK -O "$DEST/model_best_val_loss_var.pkl"
fi
echo "CubiCasa5K ready at $DEST"
