#!/usr/bin/env bash
# Fetch the trained floor-plan segmentation weights (best.pth, ~49MB) from the
# private release learned-seg-kit-v1 into tmp/models/learned_seg (git-ignored).
# Needs `gh auth login` with access to liao96312/huxingtu, plus CPU torch and
# segmentation-models-pytorch==0.5.0.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p tmp/models/learned_seg
gh release download learned-seg-kit-v1 --repo liao96312/huxingtu -p best.pth -D tmp/models/learned_seg --clobber
echo "learned_seg weights ready at tmp/models/learned_seg/best.pth"
