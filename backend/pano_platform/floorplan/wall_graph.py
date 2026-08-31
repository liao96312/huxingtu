"""Stage S3: vectorize the wall mask into an orthogonal planar wall graph.

Takes the structure layer from S2 and produces canonical axes
(Manhattan-regularised), wall segments snapped onto those axes, and gaps left
by door/window construction. Every snap and every extension is recorded so a
reviewer can see why a wall or a gap was proposed.

The two key disciplines from the recognition plan:

* thresholds are expressed in millimetres or in multiples of the measured wall
  width -- never in the stored pixel size of the drawing;
* door and window gaps are found here as *missing wall* and handed to stage S5
  for classification, rather than being proposed from decorative arcs first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .preprocess import Layers, StrokeProfile


@dataclass(frozen=True)
class AxisCluster:
    """A canonical coordinate for a family of parallel walls.

    ``value`` is the position of the axis in pixels (y for horizontal walls,
    x for vertical walls). ``members`` are the raw positions that fell into the
    cluster, useful for diagnostics and for weighting the canonical value.
    """

    value: float
    members: tuple[float, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WallSegment:
    """A piece of wall lying on a canonical axis.

    ``start``/``end`` are pixel coordinates along the axis. ``axis`` is
    ``"horizontal"`` or ``"vertical"``. ``thickness_px`` is the measured wall
    width. ``virtual`` marks a segment whose end had to be extended toward the
    next orthogonal axis: the *missing* portion is a door/window candidate that
    stage S5 classifies, so it is not treated as solid structure here.
    """

    id: str
    axis: str
    start: tuple[float, float]
    end: tuple[float, float]
    thickness_px: float
    virtual: bool


@dataclass(frozen=True)
class GapCandidate:
    """Missing wall between a virtual segment end and the axis it reached."""

    wall_edge_id: str
    axis: str
    gap_start: tuple[float, float]
    gap_end: tuple[float, float]
    width_px: float


@dataclass(frozen=True)
class WallGraph:
    """The vectorized planar wall graph plus the gap candidates for stage S5."""

    horizontal_axes: tuple[float, ...]
    vertical_axes: tuple[float, ...]
    segments: tuple[WallSegment, ...]
    gaps: tuple[GapCandidate, ...]
    mm_per_px: float
    wall_width_px: float = 0.0

    @property
    def solid_segments(self) -> tuple[WallSegment, ...]:
        return tuple(segment for segment in self.segments if not segment.virtual)


def build_wall_graph(
    layers: Layers,
    strokes: StrokeProfile,
    max_corner_snap_mm: float = 300.0,
    min_opening_width_mm: float = 300.0,
    max_opening_width_mm: float = 3000.0,
    axis_tolerance_factor: float = 1.5,
) -> WallGraph:
    """Vectorize ``layers.wall`` into canonical axes, snapped segments and gaps.

    Short endpoint gaps toward an orthogonal wall are corner cleanup only.
    Door/window candidates come from missing spans between two collinear solid
    wall runs on the same canonical axis.
    """
    if layers.wall.sum() == 0 or strokes.wall_width_px <= 0:
        return WallGraph((), (), (), (), strokes.mm_per_px)

    axis_tol = max(strokes.wall_width_px, strokes.wall_radius_px) * axis_tolerance_factor
    extension_budget_px = max_corner_snap_mm / strokes.mm_per_px if strokes.mm_per_px > 0 else 0.0

    horizontal_runs = _axis_runs(layers.horizontal, same_axis="horizontal")
    vertical_runs = _axis_runs(layers.vertical, same_axis="vertical")

    horizontal_axes = _cluster_axes(horizontal_runs, axis_tol)
    vertical_axes = _cluster_axes(vertical_runs, axis_tol)

    segments: list[WallSegment] = []
    gaps: list[GapCandidate] = []
    counter = 1

    for run in horizontal_runs:
        axis_value = _nearest_axis(run.position, horizontal_axes)
        thickness = run.thickness_px
        segment_id = f"wall_h{counter}"
        counter += 1
        solid_start, solid_end = run.start, run.end
        # Extend toward the nearest vertical axis on each side (budget-clamped).
        left = _extend_toward(solid_start, axis_value, vertical_axes, "left", extension_budget_px, run.axis)
        right = _extend_toward(solid_end, axis_value, vertical_axes, "right", extension_budget_px, run.axis)
        start = (left.target, axis_value) if left.axis == "vertical" else (solid_start, axis_value)
        end = (right.target, axis_value) if right.axis == "vertical" else (solid_end, axis_value)
        segment = WallSegment(segment_id, "horizontal", start, end, thickness, False)
        segments.append(segment)

    for run in vertical_runs:
        axis_value = _nearest_axis(run.position, vertical_axes)
        thickness = run.thickness_px
        segment_id = f"wall_v{counter}"
        counter += 1
        solid_start, solid_end = run.start, run.end
        top = _extend_toward(solid_start, axis_value, horizontal_axes, "left", extension_budget_px, run.axis)
        bottom = _extend_toward(solid_end, axis_value, horizontal_axes, "right", extension_budget_px, run.axis)
        start = (axis_value, top.target) if top.axis == "horizontal" else (axis_value, solid_start)
        end = (axis_value, bottom.target) if bottom.axis == "horizontal" else (axis_value, solid_end)
        segment = WallSegment(segment_id, "vertical", start, end, thickness, False)
        segments.append(segment)

    gap_segments, collinear_gaps = _collinear_gap_segments(
        horizontal_runs, vertical_runs, horizontal_axes, vertical_axes,
        strokes.mm_per_px, counter, min_opening_width_mm, max_opening_width_mm,
    )
    segments.extend(gap_segments)
    gaps.extend(collinear_gaps)
    boundary_segments = _ensure_outer_boundary(
        layers.wall, strokes.wall_width_px, horizontal_axes, vertical_axes, counter
    )
    segments.extend(boundary_segments)

    return WallGraph(
        horizontal_axes=horizontal_axes,
        vertical_axes=vertical_axes,
        segments=tuple(segments),
        gaps=tuple(gaps),
        mm_per_px=strokes.mm_per_px,
        wall_width_px=strokes.wall_width_px,
    )


def _ensure_outer_boundary(
    wall_mask: np.ndarray,
    wall_width_px: float,
    horizontal_axes: tuple[float, ...],
    vertical_axes: tuple[float, ...],
    counter: int,
) -> list[WallSegment]:
    """Add missing plan perimeter edges so flood-fill can form enclosed rooms."""

    ys, xs = np.where(wall_mask > 0)
    if xs.size == 0 or ys.size == 0:
        return []
    left, right = float(xs.min()), float(xs.max())
    top, bottom = float(ys.min()), float(ys.max())
    if right - left < 20 or bottom - top < 20:
        return []
    tolerance = max(2.0, wall_width_px * 1.5)
    horizontal = [
        segment for segment in horizontal_axes
        if abs(segment - top) <= tolerance or abs(segment - bottom) <= tolerance
    ]
    vertical = [
        segment for segment in vertical_axes
        if abs(segment - left) <= tolerance or abs(segment - right) <= tolerance
    ]
    result: list[WallSegment] = []
    next_id = counter
    if not any(abs(segment - top) <= tolerance for segment in horizontal):
        result.append(WallSegment(f"wall_boundary_h{next_id}", "horizontal", (left, top), (right, top), wall_width_px, False)); next_id += 1
    if not any(abs(segment - bottom) <= tolerance for segment in horizontal):
        result.append(WallSegment(f"wall_boundary_h{next_id}", "horizontal", (left, bottom), (right, bottom), wall_width_px, False)); next_id += 1
    if not any(abs(segment - left) <= tolerance for segment in vertical):
        result.append(WallSegment(f"wall_boundary_v{next_id}", "vertical", (left, top), (left, bottom), wall_width_px, False)); next_id += 1
    if not any(abs(segment - right) <= tolerance for segment in vertical):
        result.append(WallSegment(f"wall_boundary_v{next_id}", "vertical", (right, top), (right, bottom), wall_width_px, False));
    return result


def _collinear_gap_segments(
    horizontal_runs: list[_AxisRun],
    vertical_runs: list[_AxisRun],
    horizontal_axes: tuple[float, ...],
    vertical_axes: tuple[float, ...],
    mm_per_px: float,
    counter: int,
    min_width_mm: float,
    max_width_mm: float,
) -> tuple[list[WallSegment], list[GapCandidate]]:
    if mm_per_px <= 0:
        return [], []
    minimum_px, maximum_px = min_width_mm / mm_per_px, max_width_mm / mm_per_px
    virtual_segments: list[WallSegment] = []
    gaps: list[GapCandidate] = []

    for axis_name, runs, axes in (
        ("horizontal", horizontal_runs, horizontal_axes),
        ("vertical", vertical_runs, vertical_axes),
    ):
        grouped: dict[float, list[_AxisRun]] = {}
        for run in runs:
            grouped.setdefault(_nearest_axis(run.position, axes), []).append(run)
        for axis_value, axis_runs in grouped.items():
            ordered = sorted(axis_runs, key=lambda item: (item.start, item.end))
            if not ordered:
                continue
            previous_end = ordered[0].end
            previous_thickness = ordered[0].thickness_px
            for run in ordered[1:]:
                if run.start <= previous_end:
                    previous_end = max(previous_end, run.end)
                    previous_thickness = max(previous_thickness, run.thickness_px)
                    continue
                width = run.start - previous_end
                if minimum_px <= width <= maximum_px:
                    identifier = f"wall_gap{counter}"
                    counter += 1
                    start = (previous_end, axis_value) if axis_name == "horizontal" else (axis_value, previous_end)
                    end = (run.start, axis_value) if axis_name == "horizontal" else (axis_value, run.start)
                    thickness = (previous_thickness + run.thickness_px) / 2
                    virtual_segments.append(WallSegment(identifier, axis_name, start, end, thickness, True))
                    gaps.append(GapCandidate(identifier, axis_name, start, end, width))
                previous_end = run.end
                previous_thickness = run.thickness_px
    return virtual_segments, gaps


@dataclass(frozen=True)
class _AxisRun:
    """A contiguous band of parallel-structure pixels collapsed to a primitive."""

    position: float
    start: float
    end: float
    thickness_px: float
    axis: str


def _axis_runs(wall_layer: np.ndarray, same_axis: str) -> list[_AxisRun]:
    """Decompose a wall band mask into ``_AxisRun`` primitives.

    ``wall_layer`` is the ``horizontal`` or ``vertical`` mask from stage S2. Each
    connected component is one wall band; we collapse it to a single point on the
    perpendicular coordinate (its position on the canonical axis) and to a span
    along the axis.
    """
    num, labels = cv2.connectedComponents(wall_layer.astype(np.uint8), connectivity=8)
    runs: list[_AxisRun] = []
    for label in range(1, num):
        ys, xs = np.where(labels == label)
        if ys.size == 0:
            continue
        if same_axis == "horizontal":
            position = float(np.median(ys))
            start, end = float(xs.min()), float(xs.max())
            thickness = float(_modal_run(ys, xs, "x"))
        else:
            position = float(np.median(xs))
            start, end = float(ys.min()), float(ys.max())
            thickness = float(_modal_run(xs, ys, "y"))
        if start == end:
            continue
        runs.append(_AxisRun(position, start, end, thickness, same_axis))
    return runs


def _modal_run(perp: np.ndarray, along: np.ndarray, along_axis: str) -> float:
    """Modal thickness of a wall band, measured across the band.

    The band is a layout of pixels; ``perp`` is the coordinate across the wall
    (constant-ish for a straight band) and ``along`` runs along it. We bucket by
    the across-coordinate and take the run extent to recover thickness.
    """
    if perp.size == 0 or along.size == 0:
        return 0.0
    # Measure the across-wall span at each position along the component, then
    # take the modal span. The old implementation returned the most frequent
    # absolute x/y coordinate, which made wall thickness depend on page position.
    along_values = along.astype(int)
    thicknesses = []
    for coordinate in np.unique(along_values):
        cross_section = perp[along_values == coordinate]
        if cross_section.size:
            thicknesses.append(max(1, int(round(float(cross_section.max() - cross_section.min() + 1)))))
    if not thicknesses:
        return 0.0
    counts = np.bincount(np.asarray(thicknesses, dtype=int))
    return float(np.argmax(counts))


def _cluster_axes(runs: list[_AxisRun], tolerance: float) -> tuple[float, ...]:
    """1D cluster the band positions into canonical axes (Manhattan regularisation).

    Positions closer than ``tolerance`` are merged; the canonical value is the
    mean of the members (so longer walls weight an axis more naturally than a
    singleton).
    """
    if not runs:
        return ()
    positions = np.array([run.position for run in runs])
    order = np.argsort(positions)
    sorted_positions = positions[order]
    clusters: list[list[float]] = []
    current = [sorted_positions[0]]
    for value in sorted_positions[1:]:
        if value - current[-1] <= tolerance:
            current.append(value)
        else:
            clusters.append(current)
            current = [value]
    clusters.append(current)
    return tuple(float(np.mean(cluster)) for cluster in clusters)


def _nearest_axis(position: float, axes: tuple[float, ...]) -> float:
    return min(axes, key=lambda axis: abs(axis - position))


@dataclass(frozen=True)
class _Extension:
    # ``target`` is the wall segment's actual endpoint in this direction.
    # ``reached`` is the far end of any gap: equal to ``target`` when the wall
    # snaps all the way, or the otherwise-unreached orthogonal axis when virtual.
    target: float
    reached: float
    axis: str
    virtual: bool


def _extend_toward(
    coordinate: float,
    axis_value: float,
    orthogonal_axes: tuple[float, ...],
    side: str,
    budget_px: float,
    this_axis: str,
) -> _Extension:
    """Extend a wall band end toward the nearest orthogonal axis.

    ``coordinate`` is the end of the band along its own axis. We look for the
    nearest orthogonal axis at or beyond that end. If the gap is within budget
    we snap to it; otherwise we report a virtual extension (a gap candidate).
    """
    if not orthogonal_axes:
        return _Extension(coordinate, coordinate, this_axis, False)
    if side == "left":
        candidates = [axis for axis in orthogonal_axes if axis <= coordinate]
    else:
        candidates = [axis for axis in orthogonal_axes if axis >= coordinate]
    if not candidates:
        return _Extension(coordinate, coordinate, this_axis, False)
    target = min(candidates, key=lambda axis: abs(axis - coordinate))
    gap = abs(target - coordinate)
    if budget_px > 0 and gap <= budget_px:
        return _Extension(target, target, this_axis, False)
    # Over budget: the wall genuinely stops short of the orthogonal axis. The wall
    # segment endpoint stays at ``coordinate``; the gap's far end is the axis the
    # wall was heading toward.
    return _Extension(coordinate, target, this_axis, True)
