"""Optional learned floor-plan segmentation (U-Net/ResNet34, trained on ResPlan).

Loads ``tmp/models/learned_seg/best.pth`` (or ``PANO_LEARNED_SEG``) produced by
``training/learned_seg/train_seg.py``. Needs ``torch`` and
``segmentation_models_pytorch``; runs on CPU. Any failure (no weights, no
torch, bad file) returns ``None`` so the CV pipeline is used unchanged.

Outputs per pixel: class 0 background, 1 wall, 2 door, 3 window, 4 room,
plus a room-boundary mask that separates rooms without a wall between them.
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT = _ROOT / "tmp" / "models" / "learned_seg" / "best.pth"
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD = np.array([0.229, 0.224, 0.225], np.float32)
_STATE: dict = {}


def _load():
    if "model" in _STATE or _STATE.get("failed"):
        return _STATE.get("model")
    path = Path(os.environ.get("PANO_LEARNED_SEG") or _DEFAULT)
    try:
        if not path.exists():
            raise FileNotFoundError(path)
        import segmentation_models_pytorch as smp
        import torch

        checkpoint = torch.load(str(path), map_location="cpu", weights_only=False)
        classes = int(checkpoint.get("classes", 5))
        model = smp.Unet(encoder_name="resnet34", encoder_weights=None, classes=classes + 1)
        model.load_state_dict({k: v.float() for k, v in checkpoint["model"].items()})
        model.eval()
        _STATE.update(model=model, size=int(checkpoint.get("input_size", 512)), classes=classes)
    except Exception:
        _STATE["failed"] = True
        return None
    return _STATE["model"]


def segment(image_bgr: np.ndarray | None):
    """Return ``(classes, boundary)`` uint8 maps at the input size, or ``None``."""

    model = _load()
    if model is None or image_bgr is None:
        return None
    try:
        import torch

        size = _STATE["size"]
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY) if image_bgr.ndim == 3 else image_bgr
        height, width = gray.shape
        side = max(height, width)
        canvas = np.full((side, side), 255, np.uint8)  # white letterbox, like the renders
        canvas[:height, :width] = gray
        small = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
        rgb = np.repeat(small[..., None], 3, axis=2)
        tensor = torch.from_numpy(((rgb - _MEAN) / _STD).transpose(2, 0, 1).copy())[None]
        with torch.no_grad():
            logits = model(tensor)
            logits = torch.nn.functional.interpolate(logits, size=(side, side), mode="bilinear", align_corners=False)[0]
        classes = logits[: _STATE["classes"]].argmax(0).numpy().astype(np.uint8)[:height, :width]
        boundary = (logits[_STATE["classes"]] > 0).numpy().astype(np.uint8)[:height, :width]
        return classes, boundary
    except Exception:
        return None


def learned_barrier(image_bgr: np.ndarray | None) -> np.ndarray | None:
    """Wall/door/window plus room-boundary pixels, for the raster room flood."""

    result = segment(image_bgr)
    if result is None:
        return None
    classes, boundary = result
    return (((classes >= 1) & (classes <= 3)) | (boundary > 0)).astype(np.uint8)
