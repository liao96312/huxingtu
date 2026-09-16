"""Stage S0-S2: binarization, stroke-width analysis, layer separation, gate.

Every downstream threshold is expressed in millimetres or in multiples of the
measured wall stroke radius. Nothing here may depend on the stored pixel size of
the image: the same drawing exported at 800px and at 1600px must produce the
same walls, rooms and openings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

# A residential interior partition is ~100-120mm; we use the midpoint to turn a
# measured wall stroke width in pixels into a millimetre scale when no dimension
# string has been read off the drawing yet.
INTERIOR_PARTITION_MM = 110.0

# Minimum length, in multiples of the wall width, for a run of wall pixels to
# count as an axis-aligned band rather than an incidental blob.
BAND_LENGTH_FACTOR = 6

# Legibility gate. These replace the old "short edge < 1200px" rule, which
# rejected nine visually clear ~800px samples outright.
MIN_WALL_RADIUS_PX = 1.5
MIN_INK_FRACTION = 0.01
MAX_INK_FRACTION = 0.15
MIN_MANHATTAN_CONFIDENCE = 0.90


@dataclass(frozen=True)
class Binarization:
    """Ink mask plus the thresholds that produced it."""

    ink: np.ndarray
    otsu_threshold: float
    ink_fraction: float


@dataclass(frozen=True)
class StrokeProfile:
    """Stroke-width statistics of a binarized drawing.

    ``wall_radius_px`` splits the drawing into a structure layer (walls) and an
    annotation layer (dimension lines, text, furniture, window panes, door
    arcs). On real floorplans the stroke-radius histogram is strongly bimodal,
    so the split is found with Otsu rather than hard-coded.
    """

    radius: np.ndarray
    histogram: np.ndarray
    wall_radius_px: float
    wall_width_px: float
    mm_per_px: float

    @property
    def band_length_px(self) -> int:
        """Shortest run of wall pixels that still counts as a wall band."""
        return max(8, int(round(self.wall_width_px * BAND_LENGTH_FACTOR)))


@dataclass(frozen=True)
class Layers:
    """The drawing split into load-bearing structure and everything else.

    The annotation layer is not discarded: door arcs, door leaves and window
    panes all live there, and stage S5 classifies wall gaps by looking for them.
    """

    wall: np.ndarray
    annotation: np.ndarray
    horizontal: np.ndarray
    vertical: np.ndarray

    @property
    def bands(self) -> np.ndarray:
        return ((self.horizontal | self.vertical) > 0).astype(np.uint8)


@dataclass(frozen=True)
class LegibilityGate:
    """Whether a drawing is legible enough to parse automatically."""

    accepted: bool
    reason_codes: list[str] = field(default_factory=list)
    manhattan_confidence: float = 0.0
    ink_fraction: float = 0.0
    wall_radius_px: float = 0.0


def to_grayscale(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def binarize(grayscale: np.ndarray) -> Binarization:
    """Separate ink from paper.

    Scanned and re-compressed floorplans put anti-aliased wall edges anywhere in
    the 60-150 grey range, so a fixed cut (the old ``< 80``) silently drops wall
    pixels. Otsu picks the global cut. A local adaptive pass then recovers faded
    continuations, but only where it touches ink Otsu already found - used on its
    own it invents structure inside large white areas.
    """
    blurred = cv2.GaussianBlur(grayscale, (3, 3), 0)
    threshold, seed = cv2.threshold(blurred, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    block = _odd(max(15, min(grayscale.shape[:2]) // 12))
    permissive = cv2.adaptiveThreshold(
        blurred, 1, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 8
    )
    ink = _reconstruct(seed.astype(np.uint8), permissive.astype(np.uint8))
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return Binarization(ink=ink, otsu_threshold=float(threshold), ink_fraction=float(ink.mean()))


def measure_strokes(ink: np.ndarray) -> StrokeProfile:
    """Derive the wall stroke radius, wall width and a millimetre scale."""
    radius = cv2.distanceTransform(ink, cv2.DIST_L2, 5)
    values = radius[radius > 0]
    if values.size == 0:
        return StrokeProfile(radius, np.zeros(2, np.int64), 0.0, 0.0, 0.0)

    bins = np.bincount(np.minimum(np.round(values).astype(int), 63), minlength=64)
    wall_radius = _otsu_split(bins)
    wall_width = _measure_wall_width(ink, radius, wall_radius)
    mm_per_px = INTERIOR_PARTITION_MM / wall_width if wall_width > 0 else 0.0
    return StrokeProfile(
        radius=radius,
        histogram=bins,
        wall_radius_px=float(wall_radius),
        wall_width_px=float(wall_width),
        mm_per_px=float(mm_per_px),
    )


def separate_layers(ink: np.ndarray, strokes: StrokeProfile) -> Layers:
    """Split the ink into the structure layer (walls) and the annotation layer."""
    wall = _thick_strokes(ink, strokes.radius, int(round(strokes.wall_radius_px)))
    length = strokes.band_length_px
    horizontal = cv2.morphologyEx(wall, cv2.MORPH_OPEN, np.ones((1, length), np.uint8))
    vertical = cv2.morphologyEx(wall, cv2.MORPH_OPEN, np.ones((length, 1), np.uint8))
    annotation = (ink & (1 - wall)).astype(np.uint8)
    return Layers(wall=wall, annotation=annotation, horizontal=horizontal, vertical=vertical)


def manhattan_confidence(layers: Layers) -> float:
    """Fraction of the wall structure that is axis-aligned.

    Measured on walls rather than on raw gradients: Chinese room labels, door
    arcs and furniture glyphs are full of diagonal edges and would otherwise
    drag an orthogonal drawing below the gate.

    A plain ``horizontal | vertical`` opening under-counts, because wall
    corners and short junction stubs are orthogonal yet not long enough to
    survive the band-length opening. Morphological reconstruction of the bands
    under the wall mask recovers those corner and stub pixels, so the gate
    measures what is actually orthogonal rather than what is one long run.
    """
    total = float(layers.wall.sum())
    if total == 0:
        return 0.0
    bands = _reconstruct(layers.bands.astype(np.uint8), layers.wall.astype(np.uint8))
    return float(bands.sum()) / total


def assess_legibility(
    binarization: Binarization,
    strokes: StrokeProfile,
    layers: Layers,
) -> LegibilityGate:
    """Decide whether the drawing can be parsed automatically.

    Measures what actually blocks parsing - can we resolve a wall band, is there
    a sane amount of ink, is the structure orthogonal - instead of proxying all
    three with the stored image resolution.
    """
    reason_codes: list[str] = []
    confidence = manhattan_confidence(layers)

    if strokes.wall_radius_px < MIN_WALL_RADIUS_PX:
        reason_codes.append("WALL_STROKE_UNRESOLVED")
    if binarization.ink_fraction < MIN_INK_FRACTION:
        reason_codes.append("INK_COVERAGE_TOO_LOW")
    if binarization.ink_fraction > MAX_INK_FRACTION:
        reason_codes.append("INK_COVERAGE_TOO_HIGH")
    if confidence < MIN_MANHATTAN_CONFIDENCE:
        reason_codes.append("NON_MANHATTAN_INPUT")

    return LegibilityGate(
        accepted=not reason_codes,
        reason_codes=reason_codes,
        manhattan_confidence=confidence,
        ink_fraction=binarization.ink_fraction,
        wall_radius_px=strokes.wall_radius_px,
    )


def _odd(value: int) -> int:
    return value if value % 2 else value + 1


def _reconstruct(seed: np.ndarray, mask: np.ndarray, iterations: int = 40) -> np.ndarray:
    """Morphological reconstruction of ``seed`` under ``mask``."""
    current = (seed & mask).astype(np.uint8)
    kernel = np.ones((3, 3), np.uint8)
    for _ in range(iterations):
        grown = cv2.dilate(current, kernel) & mask
        if np.array_equal(grown, current):
            break
        current = grown
    return current


def _thick_strokes(ink: np.ndarray, radius: np.ndarray, wall_radius: int) -> np.ndarray:
    """Wall pixels: strokes at least ``wall_radius`` thick, grown back to full width."""
    if wall_radius <= 0:
        return np.zeros_like(ink)
    core = (radius >= wall_radius).astype(np.uint8)
    return _reconstruct(core, ink, iterations=wall_radius * 6 + 8)


def _otsu_split(histogram: np.ndarray) -> int:
    """Otsu's threshold over a stroke-radius histogram.

    Returns the smallest radius that belongs to the thick (wall) mode. Radius 0
    is background and never participates.
    """
    counts = histogram[1:].astype(np.float64)
    total = counts.sum()
    if total == 0:
        return 0
    levels = np.arange(1, counts.size + 1, dtype=np.float64)
    weight_low = np.cumsum(counts)
    weight_high = total - weight_low
    sum_low = np.cumsum(counts * levels)
    total_sum = sum_low[-1]
    valid = (weight_low > 0) & (weight_high > 0)
    if not valid.any():
        return 1
    mean_low = np.divide(sum_low, weight_low, out=np.zeros_like(sum_low), where=weight_low > 0)
    mean_high = np.divide(
        total_sum - sum_low, weight_high, out=np.zeros_like(sum_low), where=weight_high > 0
    )
    variance = weight_low * weight_high * (mean_low - mean_high) ** 2
    variance[~valid] = -1.0
    # +1 converts "last radius of the thin mode" into "first radius of the thick mode".
    return int(levels[int(np.argmax(variance))]) + 1


def _measure_wall_width(ink: np.ndarray, radius: np.ndarray, wall_radius: int) -> float:
    """Modal thickness of axis-aligned wall bands, in pixels.

    Thickness is measured across each band, never along it: the runs of a
    horizontal wall are sampled down its columns and vice versa. Measuring both
    directions in one pass would mix wall *lengths* into the distribution.
    """
    if wall_radius <= 0:
        return 0.0
    wall = _thick_strokes(ink, radius, wall_radius)
    if not wall.any():
        return float(wall_radius * 2)

    seed_length = max(8, wall_radius * 2 * BAND_LENGTH_FACTOR)
    horizontal = cv2.morphologyEx(wall, cv2.MORPH_OPEN, np.ones((1, seed_length), np.uint8))
    vertical = cv2.morphologyEx(wall, cv2.MORPH_OPEN, np.ones((seed_length, 1), np.uint8))

    widths: list[int] = []
    for column in horizontal.T:
        widths.extend(_runs(column))
    for row in vertical:
        widths.extend(_runs(row))
    if not widths:
        return float(wall_radius * 2)

    counts = np.bincount(np.array(widths, dtype=int))
    counts[0] = 0
    if counts.sum() == 0:
        return float(wall_radius * 2)
    return float(int(np.argmax(counts)))


def _runs(row: np.ndarray) -> list[int]:
    """Lengths of consecutive non-zero runs in a 1D array."""
    if not row.any():
        return []
    padded = np.concatenate(([0], row, [0]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [int(length) for length in (edges[1::2] - edges[0::2])]
