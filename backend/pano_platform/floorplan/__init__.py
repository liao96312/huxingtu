"""Deterministic floorplan recognition: image -> wall graph -> rooms -> openings.

The package is intentionally free of learned weights. Every stage exposes its
intermediate evidence so a reviewer can see why a wall, room or opening was
proposed, and every threshold is derived from the drawing itself rather than
from the pixel size of the file it happens to be stored in.
"""

from .preprocess import (
    Binarization,
    Layers,
    LegibilityGate,
    StrokeProfile,
    assess_legibility,
    binarize,
    manhattan_confidence,
    measure_strokes,
    separate_layers,
    to_grayscale,
)

__all__ = [
    "Binarization",
    "Layers",
    "LegibilityGate",
    "StrokeProfile",
    "assess_legibility",
    "binarize",
    "manhattan_confidence",
    "measure_strokes",
    "separate_layers",
    "to_grayscale",
]
