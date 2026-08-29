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


@dataclass(frozen=True)
class OpeningsFusionResult:
    confirmed: tuple[FusedOpening, ...]
    candidates: tuple[FusedOpening, ...]
    rejected: tuple[FusedOpening, ...]
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def arbitrate_openings(draft, graph: WallGraph, image_width: int, image_height: int) -> OpeningsFusionResult:
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
        edge = _nearest_edge(start_px, end_px, graph)
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

        gap_ok = edge["has_gap"] or edge["has_arc_evidence"] or edge["has_window_evidence"]
        same_edge_ok = True  # guaranteed by _nearest_edge returning a single edge

        overlap = _overlaps_confirmed(s_px, e_px, confirmed, tolerance_px=OVERLAP_TOLERANCE_PX)
        reasons: list[str] = []
        if not same_edge_ok:
            reasons.append("ENDPOINTS_ON_DIFFERENT_EDGES")
        if not width_ok:
            reasons.append("WIDTH_OUT_OF_PROFILE")
        if not gap_ok:
            reasons.append("NO_CV_GAP_OR_EVIDENCE")
        if overlap:
            reasons.append("OVERLAPS_CONFIRMED_OPENING")

        if not reasons:
            confirmed.append(
                FusedOpening(opening_id, opening.type.value, wall_edge_id, s_px, e_px, width_mm, "confirmed", ())
            )
        else:
            status = "candidate" if opening.type.value not in ("door", "window") else "rejected"
            candidates.append(
                FusedOpening(opening_id, opening.type.value, wall_edge_id, s_px, e_px, width_mm, status, tuple(reasons))
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


def _nearest_edge(start, end, graph: WallGraph) -> dict | None:
    """Find the single wall edge both VLM endpoints can project onto."""

    start_px = _to_px(start)
    end_px = _to_px(end)
    best = None
    # gaps (virtual segments) are exactly where doors/windows live, so consider
    # every segment -- solid and virtual -- as a candidate wall edge.
    for segment in graph.segments:
        s = _project_to_segment(start_px, segment)
        e = _project_to_segment(end_px, segment)
        if s["distance"] <= WALL_PROJECTION_TOLERANCE_PX and e["distance"] <= WALL_PROJECTION_TOLERANCE_PX:
            along_center_between = abs(e["along_span"] - s["along_span"])
            best = {
                "id": segment.id,
                "projected_start": s["projected"],
                "projected_end": e["projected"],
                "width_px": along_center_between,
                "has_gap": segment.virtual,
                "has_arc_evidence": _has_arc_evidence(graph, segment, s["projected"], e["projected"]),
                "has_window_evidence": _has_window_evidence(graph, segment),
            }
            break
    return best


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


def _has_window_evidence(graph: WallGraph, segment) -> bool:
    # A virtual segment with a gap not near any arc is a window-ish candidate.
    return segment.virtual


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
