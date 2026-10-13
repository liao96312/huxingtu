"""Optional CubiCasa5K wall segmentation as an extra room barrier.

Off by default. Enable by running ``scripts/fetch_cubicasa.sh`` (clones the
CubiCasa5K code and downloads its pretrained weights into
``tmp/models/cubicasa5k``; nothing is committed) and installing CPU torch.
Set ``PANO_CUBICASA_DIR`` to another checkout to override the location.

CubiCasa5K code, data and weights: Copyright (c) 2019 CubiCasa, licensed
CC BY-NC 4.0 (https://github.com/CubiCasa/CubiCasa5k). Non-commercial use
only; this project is a personal, non-commercial project. Any failure (no
torch, no weights, model error) returns ``None`` and the CV path is used.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DIR = _ROOT / "tmp" / "models" / "cubicasa5k"
_WEIGHTS = "model_best_val_loss_var.pkl"
_WALL_CLASS = 2  # room channel order: Background, Outdoor, Wall, ...
_MODEL = None
_FAILED = False


def cubicasa_wall_mask(image_bgr: np.ndarray | None) -> np.ndarray | None:
    model = _load()
    if model is None or image_bgr is None:
        return None
    try:
        import cv2
        import torch

        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
        tensor = torch.from_numpy(2.0 * (rgb / 255.0) - 1.0).permute(2, 0, 1)[None]
        with torch.no_grad():
            output = model(tensor)[0]
        rooms = output[21:33, : rgb.shape[0], : rgb.shape[1]].argmax(0).numpy()
        return (rooms == _WALL_CLASS).astype(np.uint8)
    except Exception:
        return None


def _load():
    global _MODEL, _FAILED
    if _MODEL is not None or _FAILED:
        return _MODEL
    folder = Path(os.environ.get("PANO_CUBICASA_DIR") or _DEFAULT_DIR)
    weights = folder / _WEIGHTS
    if not weights.exists():
        _FAILED = True
        return None
    try:
        import torch

        sys.path.insert(0, str(folder))
        from floortrans.models.hg_furukawa_original import hg_furukawa_original

        # Skip init_weights(): it loads an ImageNet .pth by relative path and
        # every parameter is overwritten by the checkpoint below anyway.
        model = hg_furukawa_original(n_classes=51)
        model.conv4_ = torch.nn.Conv2d(256, 44, bias=True, kernel_size=1)
        model.upsample = torch.nn.ConvTranspose2d(44, 44, kernel_size=4, stride=4)
        state = torch.load(str(weights), map_location="cpu", weights_only=False)["model_state"]
        model.load_state_dict(state)
        model.eval()
        _MODEL = model
    except Exception:
        _FAILED = True
    return _MODEL
