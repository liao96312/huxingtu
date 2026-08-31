"""Stage T07, F3: arbitrate VLM openings against the CV wall graph.

Every VLM-proposed opening is projected onto the nearest CV wall edge. It is
only confirmed if *all* of these hold:

1. both endpoints project onto the SAME wall edge;
2. the CV wall band has a corresponding gap (virtual segment) or a strong
   door-arc / window-line evidence marker;
3. the opening width falls within the physical profile;
4. it does not overlap an already-confirmed opening.

An opening that fails any check is recorded with a reason code and routed to a
candidate set rather than the ``LayoutRevision``. Nothing is published silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .fusion_evidence import FusionEvidence
from .wall_graph import WallGraph

DOOR_WIDTH_MIN_MM = 500.0
DOOR_WIDTH_MAX_MM = 1500.0
WINDOW_WIDTH_MIN_MM = 300.0
WINDOW_WIDTH_MAX_MM = 3000.0
WALL_PROJECTION_TOLERANCE_PX = 12.0
OVERLAP_TOLERANCE_PX = 8.0


@dataclass(frozen=True)
class FusedOpening:
    """An opening that has been validated against the wall graph."""

    id: str
    type: str  # door / window / doorway
    wall_edge_id: str
    start_px: tuple[float, float]
    end_px: tuple[float, float]
    width_mm: float | None
    status: str  # "confirmed" | "candidate" | "rejected"
    reason_codes: tuple[str, ...]
    side_room_ids: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())


@dataclass(frozen=True)
class OpeningsFusionResult:
    confirmed: tuple[FusedOpening, ...]
    candidates: tuple[FusedOpening, ...]
    rejected: tuple[FusedOpening, ...]
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def arbitrate_openings(
    draft, graph: WallGraph, image_width: int, image_height: int, *, annotation_mask=None
) -> OpeningsFusionResult:
    """Arbitrate the VLM draft's openings against the CV wall graph.

    ``draft`` is a ``VlmDraft``; only its ``openings`` list is consumed here.
    ``image_width``/``image_height`` de-normalise the VLM [0,1] endpoints to
    source pixels so they can be projected onto the wall graph.
    """

    confirmed: list[FusedOpening] = []
    candidates: list[FusedOpening] = []
    rejected: list[FusedOpening] = []
    reason_codes: list[str] = []

    for index, opening in enumerate(draft.openings):
        opening_id = f"vlm_opening_{index:03d}"
        start_px = _denorm(opening.pixelStart, image_width, image_height)
        end_px = _denorm(opening.pixelEnd, image_width, image_height)
        edge = _nearest_edge(start_px, end_px, graph, annotation_mask)
        if edge is None:
            rejected.append(
                FusedOpening(opening_id, opening.type.value, "", start_px, end_px, None, "rejected", ("NO_MATCHING_WALL_EDGE",))
            )
            reason_codes.append("NO_MATCHING_WALL_EDGE")
            continue

        wall_edge_id = edge["id"]
        s_px, e_px = edge["projected_start"], edge["projected_end"]
        width_px = edge["width_px"]
        width_mm = width_px * graph.mm_per_px if graph.mm_per_px > 0 else None

        width_min, width_max = _width_bounds(opening.type.value)
        width_ok = width_mm is not None and width_min <= width_mm <= width_max

        visual_evidence = {item.value for item in opening.evidence}
        same_edge_ok = True  # guaranteed by _nearest_edge returning a single edge
        side_a, side_b = _adjacent_room_ids(draft, edge, image_width, image_height)

        overlap = _overlaps_confirmed(s_px, e_px, confirmed, tolerance_px=OVERLAP_TOLERANCE_PX)
        reasons: list[str] = []
        if not same_edge_ok:
            reasons.append("ENDPOINTS_ON_DIFFERENT_EDGES")
        if not width_ok:
            reasons.append("WIDTH_OUT_OF_PROFILE")
        if not edge["has_gap"]:
            reasons.append("NO_CV_PHYSICAL_GAP")
        elif opening.type.value == "door" and not (
            edge["has_arc_evidence"] or {"doorArc", "doorLeaf"} & visual_evidence
        ):
            reasons.append("NO_DOOR_ARC_OR_LEAF_EVIDENCE")
        elif opening.type.value == "window" and not (
            edge["has_window_evidence"] or "parallelLines" in visual_evidence
        ):
            reasons.append("NO_WINDOW_PARALLEL_LINE_EVIDENCE")
        if overlap:
            reasons.append("OVERLAPS_CONFIRMED_OPENING")
        if not side_a and not side_b:
            reasons.append("NO_ADJACENT_ROOM")
        elif side_a & side_b:
            reasons.append("SAME_ROOM_BOTH_SIDES")
        elif opening.type.value == "window" and side_a and side_b:
            reasons.append("WINDOW_BETWEEN_INTERIOR_ROOMS")

        if not reasons:
            confirmed.append(
                FusedOpening(
                    opening_id, opening.type.value, wall_edge_id, s_px, e_px,
                    width_mm, "confirmed", (),
                    (tuple(sorted(side_a)), tuple(sorted(side_b))),
                )
            )
        else:
            status = "candidate" if opening.type.value not in ("door", "window") else "rejected"
            candidates.append(
                FusedOpening(
                    opening_id, opening.type.value, wall_edge_id, s_px, e_px,
                    width_mm, status, tuple(reasons),
                    (tuple(sorted(side_a)), tuple(sorted(side_b))),
                )
            )
            if status == "rejected":
                reason_codes.extend(reasons)

    return OpeningsFusionResult(
        confirmed=tuple(confirmed),
        candidates=tuple(candidates),
        rejected=tuple(rejected),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def _denorm(point, image_width: int, image_height: int) -> tuple[float, float]:
    px, py = _to_px(point)
    return px * image_width, py * image_height


def _nearest_edge(start, end, graph: WallGraph, annotation_mask=None) -> dict | None:
    """Find the single wall edge both VLM endpoints can project onto."""

    start_px = _to_px(start)
    end_px = _to_px(end)
    candidates = []
    # gaps (virtual segments) are exactly where doors/windows live, so consider
    # every segment -- solid and virtual -- as a candidate wall edge.
    for segment in graph.segments:
        s = _project_to_segment(start_px, segment)
        e = _project_to_segment(end_px, segment)
        if s["distance"] <= WALL_PROJECTION_TOLERANCE_PX and e["distance"] <= WALL_PROJECTION_TOLERANCE_PX:
            along_center_between = abs(e["along_span"] - s["along_span"])
            projected_start = segment.start if segment.virtual else s["projected"]
            projected_end = segment.end if segment.virtual else e["projected"]
            width_px = (
                ((segment.end[0] - segment.start[0]) ** 2 + (segment.end[1] - segment.start[1]) ** 2) ** 0.5
                if segment.virtual else along_center_between
            )
            candidates.append({
                "id": segment.id,
                "projected_start": projected_start,
                "projected_end": projected_end,
                "width_px": width_px,
                "has_gap": segment.virtual,
                "has_arc_evidence": _has_arc_evidence(graph, segment, s["projected"], e["projected"]),
                "has_window_evidence": _has_window_evidence(annotation_mask, segment),
                "axis": segment.axis,
                "thickness_px": segment.thickness_px,
                "_score": (0 if segment.virtual else 1, s["distance"] + e["distance"]),
            })
    if not candidates:
        return None
    best = min(candidates, key=lambda item: item["_score"])
    best.pop("_score", None)
    return best


def _adjacent_room_ids(draft, edge: dict, image_width: int, image_height: int) -> tuple[set[str], set[str]]:
    if not draft.rooms:
        return set(), set()
    try:
        from shapely.geometry import Point, Polygon
    except Exception:
        return set(), set()
    start, end = edge["projected_start"], edge["projected_end"]
    midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    distance = max(8.0, float(edge.get("thickness_px") or 0) * 1.5)
    normal = (0.0, 1.0) if edge.get("axis") == "horizontal" else (1.0, 0.0)
    samples = (
        Point(midpoint[0] + normal[0] * distance, midpoint[1] + normal[1] * distance),
        Point(midpoint[0] - normal[0] * distance, midpoint[1] - normal[1] * distance),
    )
    sides = [set(), set()]
    for index, room in enumerate(draft.rooms):
        polygon = Polygon([(point.x * image_width, point.y * image_height) for point in room.polygon])
        if not polygon.is_valid:
            continue
        for side, sample in enumerate(samples):
            if polygon.covers(sample):
                sides[side].add(f"vlm_room_{index:03d}")
    return sides[0], sides[1]


def _project_to_segment(point: tuple[float, float], segment) -> dict:
    start = segment.start
    end = segment.end
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return {"distance": float("inf"), "projected": point, "along_span": 0.0}
    along = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq))
    projected = (start[0] + along * dx, start[1] + along * dy)
    distance = ((point[0] - projected[0]) ** 2 + (point[1] - projected[1]) ** 2) ** 0.5
    span = along * (length_sq ** 0.5)
    return {"distance": distance, "projected": projected, "along_span": span}


def _has_arc_evidence(graph: WallGraph, segment, start: tuple[float, float], end: tuple[float, float]) -> bool:
    # Simplest deterministic cue: the segment is near a CV gap candidate.
    for gap in graph.gaps:
        if gap.wall_edge_id == segment.id:
            gs, ge = _to_px(gap.gap_start), _to_px(gap.gap_end)
            if _segments_near(gs, ge, start, end, tolerance=8.0):
                return True
    return False


def _has_window_evidence(annotation_mask, segment) -> bool:
    """Find two or more long annotation strokes parallel to a physical gap."""

    if annotation_mask is None or not segment.virtual:
        return False
    import numpy as np

    height, width = annotation_mask.shape[:2]
    margin = max(6, int(round(segment.thickness_px * 2)))
    if segment.axis == "horizontal":
        x1, x2 = sorted((int(round(segment.start[0])), int(round(segment.end[0]))))
        y = int(round(segment.start[1]))
        crop = annotation_mask[max(0, y - margin):min(height, y + margin + 1), max(0, x1):min(width, x2 + 1)]
        if crop.size == 0 or crop.shape[1] < 3:
            return False
        coverage = crop.sum(axis=1) / crop.shape[1]
    else:
        y1, y2 = sorted((int(round(segment.start[1])), int(round(segment.end[1]))))
        x = int(round(segment.start[0]))
        crop = annotation_mask[max(0, y1):min(height, y2 + 1), max(0, x - margin):min(width, x + margin + 1)]
        if crop.size == 0 or crop.shape[0] < 3:
            return False
        coverage = crop.sum(axis=0) / crop.shape[0]
    strong = np.flatnonzero(coverage >= 0.55)
    if strong.size < 2:
        return False
    clusters = 1 + int(np.count_nonzero(np.diff(strong) > 1))
    return clusters >= 2


def _segments_near(a1, a2, b1, b2, tolerance: float) -> bool:
    def _mid(p, q):
        return ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)

    ma, mb = _mid(a1, a2), _mid(b1, b2)
    return ((ma[0] - mb[0]) ** 2 + (ma[1] - mb[1]) ** 2) ** 0.5 <= tolerance


def _overlaps_confirmed(start: tuple[float, float], end: tuple[float, float], confirmed: list, tolerance_px: float) -> bool:
    for existing in confirmed:
        if _segments_overlap(start, end, existing.start_px, existing.end_px, tolerance_px):
            return True
    return False


def _segments_overlap(a1, a2, b1, b2, tolerance_px: float) -> bool:
    # project all onto a dominant axis and check interval overlap
    axis = 0 if abs(a2[0] - a1[0]) >= abs(a2[1] - a1[1]) else 1
    a_lo, a_hi = sorted((a1[axis], a2[axis]))
    b_lo, b_hi = sorted((b1[axis], b2[axis]))
    return min(a_hi, b_hi) - max(a_lo, b_lo) > tolerance_px


def _to_px(point) -> tuple[float, float]:
    if hasattr(point, "x") and hasattr(point, "y"):
        return point.x, point.y
    return point[0], point[1]


def _width_bounds(type_value: str) -> tuple[float, float]:
    if type_value == "window":
        return WINDOW_WIDTH_MIN_MM, WINDOW_WIDTH_MAX_MM
    if type_value in ("door", "doorway"):
        return DOOR_WIDTH_MIN_MM, DOOR_WIDTH_MAX_MM
    return DOOR_WIDTH_MIN_MM, DOOR_WIDTH_MAX_MM
