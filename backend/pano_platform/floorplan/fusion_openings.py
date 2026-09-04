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
from math import hypot

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


def arbitrate_cv_windows(graph: WallGraph, rooms, annotation_mask) -> OpeningsFusionResult:
    """Confirm exterior windows from CV gap + parallel-line evidence."""

    confirmed: list[FusedOpening] = []
    rejected: list[FusedOpening] = []
    reason_codes: list[str] = []
    room_polygons = []
    try:
        from shapely.geometry import Polygon

        for room in rooms:
            polygon = getattr(room, "polygon_px", None) or getattr(room, "polygon", None)
            if polygon:
                room_polygons.append((getattr(room, "id", "room"), Polygon(polygon)))
    except Exception:
        room_polygons = []

    for index, segment in enumerate(item for item in graph.segments if item.virtual):
        if not _has_window_evidence(annotation_mask, segment):
            continue
        identifier = f"cv_window_{index:03d}"
        width_px = hypot(segment.end[0] - segment.start[0], segment.end[1] - segment.start[1])
        width_mm = width_px * graph.mm_per_px if graph.mm_per_px > 0 else None
        side_rooms = _room_sides_for_segment(
            segment.start, segment.end, segment, room_polygons, graph.wall_width_px
        )
        reasons: list[str] = []
        if width_mm is None or not WINDOW_WIDTH_MIN_MM <= width_mm <= WINDOW_WIDTH_MAX_MM:
            reasons.append("CV_WINDOW_WIDTH_OUT_OF_PROFILE")
        if not side_rooms[0] and not side_rooms[1]:
            reasons.append("CV_WINDOW_NO_ADJACENT_ROOM")
        elif set(side_rooms[0]) & set(side_rooms[1]):
            reasons.append("CV_WINDOW_SAME_ROOM_BOTH_SIDES")
        elif side_rooms[0] and side_rooms[1]:
            reasons.append("CV_WINDOW_BETWEEN_INTERIOR_ROOMS")
        if reasons:
            rejected.append(FusedOpening(
                identifier, "window", segment.id, segment.start, segment.end,
                width_mm, "rejected", tuple(reasons), side_rooms,
            ))
            reason_codes.extend(reasons)
        else:
            confirmed.append(FusedOpening(
                identifier, "window", segment.id, segment.start, segment.end,
                width_mm, "confirmed", ("CV_WINDOW_AUTOMATIC",), side_rooms,
            ))

    return OpeningsFusionResult(
        confirmed=tuple(confirmed), candidates=(), rejected=tuple(rejected),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def arbitrate_cv_door_arcs(
    proposals,
    graph: WallGraph,
    rooms,
    image_width: int,
    image_height: int,
    *,
    min_arc_score: float = 0.88,
) -> OpeningsFusionResult:
    """Turn existing CV door-arc proposals into topology-checked openings.

    This is the CV-only complement to :func:`arbitrate_openings`: an arc may
    open a solid WallEdge when a door leaf has covered the wall centreline, but
    it must still project to one edge, fit the door width profile, and have a
    room on at least one side. The result is deliberately door-only; windows
    still require their parallel-line evidence path.
    """

    confirmed: list[FusedOpening] = []
    rejected: list[FusedOpening] = []
    reason_codes: list[str] = []
    room_polygons = []
    try:
        from shapely.geometry import Polygon

        for room in rooms:
            polygon = getattr(room, "polygon_px", None) or getattr(room, "polygon", None)
            if polygon:
                room_polygons.append((getattr(room, "id", "room"), Polygon(polygon)))
    except Exception:
        room_polygons = []

    for index, proposal in enumerate(proposals):
        opening_id = f"cv_opening_{index:03d}"
        center = tuple(float(value) for value in proposal.center)
        if proposal.arc_score < min_arc_score:
            rejected.append(FusedOpening(
                opening_id, "door", "", center, center, None, "rejected",
                ("CV_ARC_SCORE_LOW",),
            ))
            reason_codes.append("CV_ARC_SCORE_LOW")
            continue

        nearest = []
        for segment in graph.segments:
            projected = _project_to_segment(center, segment)
            if projected["distance"] <= max(12.0, graph.wall_width_px * 2.5):
                nearest.append((projected["distance"], 0 if segment.virtual else 1, segment, projected))
        if not nearest:
            rejected.append(FusedOpening(
                opening_id, "door", "", center, center, None, "rejected",
                ("CV_ARC_NO_WALL_EDGE",),
            ))
            reason_codes.append("CV_ARC_NO_WALL_EDGE")
            continue

        _, _, segment, projected = min(nearest, key=lambda item: (item[1], item[0]))
        length = hypot(segment.end[0] - segment.start[0], segment.end[1] - segment.start[1])
        if length <= 0:
            continue
        unit = ((segment.end[0] - segment.start[0]) / length, (segment.end[1] - segment.start[1]) / length)
        subdivided_gap = False
        bootstrap_minimum_width = max(
            0.0, DOOR_WIDTH_MIN_MM * 0.7 - max(0.0, graph.mm_per_px)
        )
        if segment.virtual:
            full_width_mm = length * graph.mm_per_px if graph.mm_per_px > 0 else None
            if full_width_mm is not None and bootstrap_minimum_width <= full_width_mm <= DOOR_WIDTH_MAX_MM:
                start_px, end_px = segment.start, segment.end
                width_px = length
            else:
                # One graph gap can span two adjacent door leaves when the
                # narrow wall post between them was lost. Use each arc radius
                # as its own interval instead of treating the entire gap as one
                # oversized door.
                width_px = min(length, max(1.0, float(proposal.radius_px)))
                start_along = max(0.0, min(length - width_px, projected["along_span"] - width_px / 2.0))
                end_along = start_along + width_px
                for existing in confirmed:
                    if existing.wall_edge_id != segment.id or not _segments_overlap(
                        (segment.start[0] + unit[0] * start_along, segment.start[1] + unit[1] * start_along),
                        (segment.start[0] + unit[0] * end_along, segment.start[1] + unit[1] * end_along),
                        existing.start_px, existing.end_px, 0.0,
                    ):
                        continue
                    existing_interval = sorted((
                        _project_to_segment(existing.start_px, segment)["along_span"],
                        _project_to_segment(existing.end_px, segment)["along_span"],
                    ))
                    if projected["along_span"] >= sum(existing_interval) / 2.0:
                        start_along = max(start_along, existing_interval[1] + 1.0)
                    else:
                        end_along = min(end_along, existing_interval[0] - 1.0)
                width_px = max(0.0, end_along - start_along)
                start_px = (segment.start[0] + unit[0] * start_along, segment.start[1] + unit[1] * start_along)
                end_px = (segment.start[0] + unit[0] * end_along, segment.start[1] + unit[1] * end_along)
                subdivided_gap = True
        else:
            # A door leaf radius is the best available interval estimate when
            # the leaf has obscured the wall gap. Centre the interval at the
            # projected arc and keep it on the physical wall edge.
            # The detected circle radius is the leaf sweep, while the door
            # opening spans both sides of the projected centreline. Use the
            # full sweep as the half-width estimate and clamp it to the wall.
            half = max(1.0, float(proposal.radius_px))
            start_along = max(0.0, projected["along_span"] - half)
            end_along = min(length, projected["along_span"] + half)
            width_px = end_along - start_along
            start_px = (segment.start[0] + unit[0] * start_along, segment.start[1] + unit[1] * start_along)
            end_px = (segment.start[0] + unit[0] * end_along, segment.start[1] + unit[1] * end_along)

        width_mm = width_px * graph.mm_per_px if graph.mm_per_px > 0 else None
        reasons: list[str] = []
        # Wall-thickness scale is only a bootstrap estimate. Keep the physical
        # door profile, but tolerate its known under-scaling plus one raster
        # pixel when a strong arc is anchored to a wall and passes topology.
        minimum_width = bootstrap_minimum_width
        if width_mm is None or not minimum_width <= width_mm <= DOOR_WIDTH_MAX_MM:
            reasons.append("CV_ARC_WIDTH_OUT_OF_PROFILE")
        if not segment.virtual and proposal.arc_score < 0.96:
            reasons.append("CV_ARC_SOLID_WALL_SCORE_LOW")
        side_rooms = _room_sides_for_segment(start_px, end_px, segment, room_polygons, graph.wall_width_px)
        if not side_rooms[0] and not side_rooms[1]:
            reasons.append("CV_ARC_NO_ADJACENT_ROOM")
        elif set(side_rooms[0]) & set(side_rooms[1]) and not (
            segment.virtual and proposal.arc_score >= 0.96
        ):
            reasons.append("CV_ARC_SAME_ROOM_BOTH_SIDES")
        if any(
            existing.wall_edge_id == segment.id
            and _segments_overlap(start_px, end_px, existing.start_px, existing.end_px, 0.0)
            for existing in confirmed
        ):
            reasons.append("CV_ARC_DUPLICATE_OPENING")

        if reasons:
            rejected.append(FusedOpening(
                opening_id, "door", segment.id, start_px, end_px, width_mm,
                "rejected", tuple(reasons), side_rooms,
            ))
            reason_codes.extend(reasons)
            continue
        confirmation_codes = ["CV_ARC_AUTOMATIC"]
        if width_mm is not None and width_mm < DOOR_WIDTH_MIN_MM:
            confirmation_codes.append("CV_ARC_BOOTSTRAP_SCALE_TOLERANCE")
        if set(side_rooms[0]) & set(side_rooms[1]):
            confirmation_codes.append("CV_ARC_SHARED_ROOM_PHYSICAL_GAP")
        if subdivided_gap:
            confirmation_codes.append("CV_ARC_SUBDIVIDED_PHYSICAL_GAP")
        confirmed.append(FusedOpening(
            opening_id, "door", segment.id, start_px, end_px, width_mm,
            "confirmed", tuple(confirmation_codes), side_rooms,
        ))

    return OpeningsFusionResult(
        confirmed=tuple(confirmed), candidates=(), rejected=tuple(rejected),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


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


def _room_sides_for_segment(start, end, segment, room_polygons, wall_width_px: float) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Sample room polygons on both sides of a pixel wall segment."""

    length = hypot(end[0] - start[0], end[1] - start[1])
    if length <= 0:
        return (), ()
    midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    normal = (-(end[1] - start[1]) / length, (end[0] - start[0]) / length)
    offset = max(8.0, wall_width_px * 1.5)
    try:
        from shapely.geometry import Point

        samples = (
            Point(midpoint[0] + normal[0] * offset, midpoint[1] + normal[1] * offset),
            Point(midpoint[0] - normal[0] * offset, midpoint[1] - normal[1] * offset),
        )
        sides = [[], []]
        for room_id, polygon in room_polygons:
            for side, sample in enumerate(samples):
                if polygon.covers(sample):
                    # CV fallback can contain a conservative outer envelope
                    # that nests smaller room polygons. The smallest covering
                    # polygon is the most specific room for topology sampling;
                    # retaining every enclosing polygon makes both sides look
                    # like the same room and rejects real internal doors.
                    sides[side].append((polygon.area, room_id))
        return (
            (min(sides[0])[1],) if sides[0] else (),
            (min(sides[1])[1],) if sides[1] else (),
        )
    except Exception:
        return (), ()


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
