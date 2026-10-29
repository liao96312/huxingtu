#!/usr/bin/env bash
# Fetch the trained floor-plan segmentation weights (best.pth, ~49MB) from the
# public release learned-seg-kit-v2 (640px, 6-epoch warm start from v1) into
# tmp/models/learned_seg (git-ignored). No GitHub login needed (uses curl);
# falls back to `gh release download` only if curl is unavailable.
# Inference also needs CPU torch and segmentation-models-pytorch==0.5.0.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=tmp/models/learned_seg
URL=https://github.com/liao96312/huxingtu/releases/download/learned-seg-kit-v2/best.pth
mkdir -p "$DEST"
if command -v curl >/dev/null 2>&1; then
  curl -fL --retry 3 -o "$DEST/best.pth.part" "$URL"
  mv -f "$DEST/best.pth.part" "$DEST/best.pth"
elif command -v gh >/dev/null 2>&1; then
  gh release download learned-seg-kit-v2 --repo liao96312/huxingtu -p best.pth -D "$DEST" --clobber
else
  echo "error: need curl (or gh) to download $URL" >&2
  exit 1
fi
echo "learned_seg weights ready at $DEST/best.pth"
