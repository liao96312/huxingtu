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
    import hashlib

    key = hashlib.md5(image_bgr.tobytes()).hexdigest()
    if _STATE.get("cache_key") == key:
        return _STATE["cache_value"]
    _STATE["cache_key"], _STATE["cache_value"] = key, _segment(model, image_bgr)
    return _STATE["cache_value"]


def _segment(model, image_bgr):
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


# Tuned on the ResPlan val slice only (first 40 images); test slice untouched.
MIN_ROOM_WALL_WIDTH2 = 10.0  # ~3000 px^2 with 17 px walls on a 1024 canvas
GROW_WALL_WIDTHS = 0.4       # ~7 px (v2 weights, val-tuned): push room cells back into the wall band


def learned_room_polygons(image_bgr: np.ndarray | None, wall_width_px: float):
    """Room polygons = connected (room & not boundary) cells, or ``None``."""

    from shapely.geometry import Polygon

    result = segment(image_bgr)
    if result is None:
        return None
    classes, boundary = result
    ww = max(2.0, float(wall_width_px or 0))
    free = ((classes == 4) & (boundary == 0)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
    grow = max(1, int(round(GROW_WALL_WIDTHS * ww)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * grow + 1, 2 * grow + 1))
    polygons = []
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] < MIN_ROOM_WALL_WIDTH2 * ww * ww:
            continue
        mask = cv2.dilate((labels == label).astype(np.uint8), kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        polygon = Polygon(max(contours, key=cv2.contourArea).reshape(-1, 2)).buffer(0)
        if polygon.is_empty:
            continue
        if polygon.geom_type != "Polygon":
            polygon = max(polygon.geoms, key=lambda part: part.area)
        polygon = polygon.simplify(3.0, preserve_topology=True)
        points = tuple((float(x), float(y)) for x, y in list(polygon.exterior.coords)[:-1])
        if len(points) >= 3:
            polygons.append(points)
    return polygons


MIN_OPENING_WALL_WIDTH2 = 0.3  # ~80 px^2 with 17 px walls (val-tuned)


def learned_openings(image_bgr: np.ndarray | None, wall_width_px: float):
    """Door/window spans from the model as ``(type, start, end)`` tuples, or ``None``.

    Each door (class 2) / window (class 3) component becomes one span along
    the long side of its bounding box, centred across the wall.
    """

    result = segment(image_bgr)
    if result is None:
        return None
    classes, _ = result
    ww = max(2.0, float(wall_width_px or 0))
    spans = []
    for kind, value in (("door", 2), ("window", 3)):
        count, _, stats, _ = cv2.connectedComponentsWithStats((classes == value).astype(np.uint8), connectivity=8)
        for label in range(1, count):
            x, y, w, h, area = (float(v) for v in stats[label])
            if area < MIN_OPENING_WALL_WIDTH2 * ww * ww:
                continue
            if w >= h:
                start, end = (x, y + h / 2), (x + w, y + h / 2)
            else:
                start, end = (x + w / 2, y), (x + w / 2, y + h)
            if _wall_at_both_ends(classes, start, end, ww):
                spans.append((kind, start, end))
    return spans


def _wall_at_both_ends(classes: np.ndarray, start, end, ww: float) -> bool:
    """An opening sits in a wall: wall pixels must continue past both ends.

    Rejects door/window-coloured specks on text, dimensions or furniture,
    which the model produces on real (non-ResPlan) plans.
    """

    height, width = classes.shape
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = max(1e-6, (dx * dx + dy * dy) ** 0.5)
    ux, uy = dx / length, dy / length
    reach = int(round(ww))
    for (px, py), sign in ((start, -1.0), (end, 1.0)):
        found = False
        for step in range(1, reach + 1):
            x = int(round(px + sign * ux * step))
            y = int(round(py + sign * uy * step))
            if 0 <= x < width and 0 <= y < height and classes[y, x] == 1:
                found = True
                break
        if not found:
            return False
    return True
