"""Stage T06: fuse the VLM draft onto the CV wall graph.

F1 de-normalises the VLM draft's [0,1] coordinates back to source pixels and
snaps room vertices to the nearest CV wall axis (dynamic wall-thickness
tolerance). F2 reconciles the VLM scale candidates with the CV wall-thickness
self-bootstrapped mm/px using median/MAD, and cross-checks each room's polygon
area against any printed ``areaLabelM2``.

A draft item that does not reach the threshold is not published; it is reported
with a ``review`` status and a reason code so the caller routes it to
``OpeningCandidateSet`` / review, never straight to ``LayoutRevision``.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from ..vision.contracts import VlmDraft
from .fusion_evidence import FusionEvidence, RoomSnapEvidence, ScaleEvidence
from .rooms import _polygon_area
from .wall_graph import WallGraph

MAX_AREA_RELATIVE_ERROR = 0.15
MAX_SCALE_RELATIVE_ERROR = 0.10
MAD_REJECT_FACTOR = 3.0


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


@dataclass(frozen=True)
class FusedRoom:
    """A room that has been de-normalised and, where possible, snapped to walls."""

    id: str
    type: str
    polygon_px: tuple[tuple[float, float], ...]
    area_mm2: float
    area_px2: float
    status: str  # "ok" | "review"
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class FusionResult:
    mm_per_px: float | None
    scale_status: str
    rooms: tuple[FusedRoom, ...]
    evidence: FusionEvidence


def fuse(
    draft: VlmDraft,
    graph: WallGraph,
    image_width: int,
    image_height: int,
    *,
    snap_tolerance_factor: float = 1.0,
) -> FusionResult:
    """Fuse a VLM draft onto the CV wall graph.

    ``image_width``/``image_height`` are the source image dimensions used to
    de-normalise the VLM coordinates. ``snap_tolerance_factor`` scales the wall
    thickness: a vertex within ``factor * wall_width_px`` of an axis is snapped.
    """

    mm_per_px, scale_status, scale_evidence = _reconcile_scale(draft, graph, image_width, image_height)

    fused_rooms: list[FusedRoom] = []
    room_evidence: list[RoomSnapEvidence] = []
    semantic_corrections = False
    for index, room in enumerate(draft.rooms):
        room_id = f"vlm_room_{index:03d}"
        room_type, corrected = _resolve_room_type(room.type.value, room.name)
        semantic_corrections = semantic_corrections or corrected
        polygon_px = _denormalize_polygon(room.polygon, image_width, image_height)
        snapped = _snap_polygon(polygon_px, graph, snap_tolerance_factor)
        snapped_count = sum(
            1 for a, b in zip(polygon_px, snapped) if _distance(a, b) > 1e-6
        )
        area_px2 = abs(_polygon_area(snapped))
        area_mm2 = area_px2 * mm_per_px * mm_per_px if mm_per_px else 0.0

        area_error = None
        reason_codes: list[str] = []
        if room.areaLabelM2:
            label_px2 = room.areaLabelM2 * 1e6 / (mm_per_px * mm_per_px) if mm_per_px else None
            if label_px2:
                area_error = abs(area_px2 - label_px2) / label_px2
                if area_error > MAX_AREA_RELATIVE_ERROR:
                    reason_codes.append("AREA_LABEL_MISMATCH")
        if snapped_count == 0:
            reason_codes.append("NO_VERTEX_SNAPPED")

        status = "ok" if not reason_codes else "review"
        fused_rooms.append(
            FusedRoom(
                id=room_id,
                type=room_type,
                polygon_px=snapped,
                area_mm2=area_mm2,
                area_px2=area_px2,
                status=status,
                reason_codes=tuple(reason_codes),
            )
        )
        room_evidence.append(
            RoomSnapEvidence(
                roomId=room_id,
                polygon=polygon_px,
                snappedPolygon=snapped,
                areaPx2=area_px2,
                areaMm2=area_mm2,
                areaLabelM2=room.areaLabelM2,
                areaRelativeError=area_error,
                snappedVertexCount=snapped_count,
                totalVertexCount=len(snapped),
                status=status,
                reasonCodes=tuple(reason_codes),
            )
        )

    reason_codes: list[str] = []
    if scale_status == "review":
        reason_codes.append("SCALE_UNCERTAIN")
    if any(room.status == "review" for room in fused_rooms):
        reason_codes.append("SOME_ROOMS_UNSNAPPED")
    if semantic_corrections:
        reason_codes.append("ROOM_LABEL_TYPE_CORRECTED")

    evidence = FusionEvidence(
        scale=scale_evidence,
        rooms=tuple(room_evidence),
        mmPerPixel=mm_per_px,
        scaleStatus=scale_status,
        reasonCodes=tuple(reason_codes),
    )
    return FusionResult(
        mm_per_px=mm_per_px,
        scale_status=scale_status,
        rooms=tuple(fused_rooms),
        evidence=evidence,
    )


def _resolve_room_type(model_type: str, label: str | None) -> tuple[str, bool]:
    if not label:
        return model_type, False
    normalized = label.replace(" ", "")
    rules = (
        (("客餐厅", "餐客厅"), "livingDining"),
        (("卫生间", "洗手间", "厕所"), "bathroom"),
        (("厨房",), "kitchen"),
        (("阳台",), "balcony"),
        (("书房",), "study"),
        (("卧室", "主卧", "次卧", "儿童房", "老人房"), "bedroom"),
        (("客厅",), "living"),
        (("餐厅",), "dining"),
    )
    for keywords, resolved in rules:
        if any(keyword in normalized for keyword in keywords):
            return resolved, resolved != model_type
    return model_type, False


def _reconcile_scale(
    draft: VlmDraft, graph: WallGraph, image_width: int, image_height: int
) -> tuple[float | None, str, tuple[ScaleEvidence, ...]]:
    """Reconcile VLM scale candidates with the CV wall-thickness scale."""

    cv_mm_per_px = graph.mm_per_px if graph.mm_per_px > 0 else None
    if not draft.scaleCandidates:
        return cv_mm_per_px, "bootstrap" if cv_mm_per_px is not None else "review", ()

    observations: list[float] = []
    evidence: list[ScaleEvidence] = []
    for index, candidate in enumerate(draft.scaleCandidates):
        candidate_id = f"scale_{index:03d}"
        p1x, p1y = _norm_coords(candidate.pixelStart)
        p2x, p2y = _norm_coords(candidate.pixelEnd)
        p1x, p1y = p1x * image_width, p1y * image_height
        p2x, p2y = p2x * image_width, p2y * image_height
        pixel_distance = ((p1x - p2x) ** 2 + (p1y - p2y) ** 2) ** 0.5
        if pixel_distance <= 0:
            evidence.append(
                ScaleEvidence(candidate_id, candidate.lengthMm, 0.0, 0.0, cv_mm_per_px or 0.0, 0.0, False, ("ZERO_PIXEL_DISTANCE",))
            )
            continue
        vlm_mm_per_px = candidate.lengthMm / pixel_distance
        observations.append(vlm_mm_per_px)
        evidence.append(
            ScaleEvidence(candidate_id, candidate.lengthMm, pixel_distance, vlm_mm_per_px, cv_mm_per_px or 0.0, 0.0, True)
        )

    if not observations:
        return cv_mm_per_px, "bootstrap" if cv_mm_per_px is not None else "review", tuple(evidence)

    reconciled = _robust_median(observations)
    reason_codes: list[str] = []
    status = "confirmed"
    if cv_mm_per_px is not None:
        # The CV wall-thickness bootstrap (interior partition ~110mm) is the
        # reliable anchor; the VLM's printed dimension strings are a cross-check.
        # If they disagree, prefer the CV scale but flag review so the caller does
        # not silently publish a scale that could be wrong.
        relative_error = abs(reconciled - cv_mm_per_px) / cv_mm_per_px
        evidence = _fill_cross_check(evidence, reconciled, cv_mm_per_px)
        if relative_error > MAX_SCALE_RELATIVE_ERROR:
            reason_codes.append("CV_AND_VLM_SCALE_DISAGREE")
            status = "review"
        # Trust the CV anchor: it is derived from the drawing's own wall thickness,
        # not from a possibly-misread string.
        reconciled = cv_mm_per_px
    else:
        # no CV scale to fall back on — VLM scale stands but is unverified
        status = "review"

    return reconciled, status, tuple(evidence)


def _robust_median(observations: list[float]) -> float:
    """Median after MAD-based outlier rejection.

    The VLM often emits many spurious "dimension" candidates (interior lines,
    duplicated or truncated reads). A naive median is dragged by them; a robust
    median based on the median absolute deviation (MAD) keeps only the mutually
    consistent cluster and reports its median.
    """

    if len(observations) == 1:
        return observations[0]
    center = median(observations)
    absolute_deviations = [abs(value - center) for value in observations]
    mad = median(absolute_deviations)
    if mad <= 1e-12:
        # all observations agree; that cluster IS the answer
        return center
    scale = 1.4826 * mad  # MAD -> sigma estimate
    survivors = [value for value in observations if abs(value - center) <= MAD_REJECT_FACTOR * scale]
    return median(survivors) if survivors else center


def _fill_cross_check(
    evidence: tuple[ScaleEvidence, ...],
    vlm_mm_per_px: float,
    cv_mm_per_px: float,
) -> list[ScaleEvidence]:
    updated: list[ScaleEvidence] = []
    for item in evidence:
        if item.pixelDistance <= 0:
            updated.append(item)
            continue
        relative_error = abs(item.vlmMmPerPixel - cv_mm_per_px) / cv_mm_per_px
        agreed = relative_error <= MAX_SCALE_RELATIVE_ERROR
        codes = item.reasonCodes
        if not agreed:
            codes = tuple(list(codes) + ["SCALE_DISAGREES_WITH_CV"])
        updated.append(
            ScaleEvidence(
                candidateId=item.candidateId, lengthMm=item.lengthMm,
                pixelDistance=item.pixelDistance, vlmMmPerPixel=item.vlmMmPerPixel,
                cvMmPerPixel=cv_mm_per_px, relativeError=relative_error,
                agreed=agreed, reasonCodes=codes,
            )
        )
    return updated


def _denormalize_polygon(polygon, width: int, height: int) -> tuple[tuple[float, float], ...]:
    return tuple((_norm_x(point) * width, _norm_y(point) * height) for point in polygon)


def _norm_coords(point) -> tuple[float, float]:
    """Return (nx, ny) from a NormPoint or a plain ``[x, y]``."""

    if hasattr(point, "x") and hasattr(point, "y"):
        return point.x, point.y
    return point[0], point[1]


def _norm_x(point) -> float:
    return _norm_coords(point)[0]


def _norm_y(point) -> float:
    return _norm_coords(point)[1]


def _snap_polygon(
    polygon: tuple[tuple[float, float], ...],
    graph: WallGraph,
    tolerance_factor: float,
) -> tuple[tuple[float, float], ...]:
    # Snapped-and-snapped returns the snapped coordinates, using dynamic tolerance.
    return tuple(_snap_point(point, graph, tolerance_factor) for point in polygon)


def _snap_point(
    point: tuple[float, float],
    graph: WallGraph,
    tolerance_factor: float,
) -> tuple[float, float]:
    x, y = point
    ny = _snap_to_axis(y, graph.horizontal_axes, tolerance_factor, fallback=y, wall_width_px=graph.wall_width_px)
    nx = _snap_to_axis(x, graph.vertical_axes, tolerance_factor, fallback=x, wall_width_px=graph.wall_width_px)
    return (nx, ny)


def _snap_to_axis(
    coordinate: float,
    axes: tuple[float, ...],
    tolerance_factor: float,
    fallback: float,
    wall_width_px: float = 0.0,
) -> float:
    """Snap ``coordinate`` to the nearest axis if it is within tolerance.

    The tolerance is dynamic: ``band_length_px`` already scales with the measured
    wall width, so snapping radii stay relative to the drawing rather than to the
    stored pixel size.
    """

    if not axes:
        return fallback
    nearest = min(axes, key=lambda axis: abs(axis - coordinate))
    tolerance = tolerance_factor * _axis_tolerance_px(
        abs(axes[1] - axes[0]) if len(axes) > 1 else abs(nearest) + 1.0,
        wall_width_px,
    )
    if abs(nearest - coordinate) <= tolerance:
        return nearest
    return fallback


def _axis_tolerance_px(reference: float, wall_width_px: float = 0.0) -> float:
    # Prefer measured wall width; axis spacing is only a legacy fallback.
    return max(1.0, wall_width_px * 1.5 if wall_width_px > 0 else reference * 0.2)
