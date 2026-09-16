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

from dataclasses import dataclass, field, replace
from math import hypot

from .fusion_evidence import FusionEvidence
from .wall_graph import WallGraph, WallSegment

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


def arbitrate_cv_windows(graph: WallGraph, rooms, annotation_mask, door_arcs=()) -> OpeningsFusionResult:
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
    exterior_labels, exterior_ids = _exterior_components(annotation_mask)

    for index, segment in enumerate(item for item in graph.segments if item.virtual):
        if not _has_window_evidence(annotation_mask, segment):
            continue
        identifier = f"cv_window_{index:03d}"
        width_px = hypot(segment.end[0] - segment.start[0], segment.end[1] - segment.start[1])
        width_mm = width_px * graph.mm_per_px if graph.mm_per_px > 0 else None
        side_rooms = _room_sides_for_segment(
            segment.start, segment.end, segment, room_polygons, graph.wall_width_px,
            sample_offset_factor=3.0,
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

    wall_candidates = []
    wall_candidate_index = 0
    axis_groups = {}
    for segment in (item for item in graph.segments if not item.virtual):
        fixed = segment.start[1] if segment.axis == "horizontal" else segment.start[0]
        axis_groups.setdefault((segment.axis, round(fixed, 3)), []).append(segment)
    for (axis, fixed), group in axis_groups.items():
        height, width = annotation_mask.shape[:2] if annotation_mask is not None else (0, 0)
        representative = max(group, key=lambda item: hypot(item.end[0] - item.start[0], item.end[1] - item.start[1]))
        scan_segment = replace(
            representative,
            start=(0.0, fixed) if axis == "horizontal" else (fixed, 0.0),
            end=(float(width - 1), fixed) if axis == "horizontal" else (fixed, float(height - 1)),
        )
        for start, end in _window_intervals_on_wall(annotation_mask, scan_segment, graph):
            identifier = f"cv_window_wall_candidate_{wall_candidate_index:03d}"
            wall_candidate_index += 1
            segment = max(group, key=lambda item: _interval_overlap_length(start, end, item.start, item.end))
            reasons = []
            if any(
                _door_arc_occupies_window_interval(arc, start, end, graph.wall_width_px)
                for arc in door_arcs
            ):
                reasons.append("CV_WINDOW_WALL_LINES_DOOR_ARC_OVERLAP")
            width_px = hypot(end[0] - start[0], end[1] - start[1])
            width_mm = width_px * graph.mm_per_px if graph.mm_per_px > 0 else None
            side_rooms = _room_sides_for_segment(
                start, end, segment, room_polygons, graph.wall_width_px,
                sample_offset_factor=3.0,
            )
            exterior = _pixel_exterior_sides(
                exterior_labels, exterior_ids, start, end, graph.wall_width_px
            )
            bootstrap_minimum_width = max(
                0.0, WINDOW_WIDTH_MIN_MM * 0.8 - max(0.0, graph.mm_per_px)
            ) if exterior[0] != exterior[1] else WINDOW_WIDTH_MIN_MM
            if width_mm is None or not bootstrap_minimum_width <= width_mm <= WINDOW_WIDTH_MAX_MM:
                reasons.append("CV_WINDOW_WALL_LINES_WIDTH_OUT_OF_PROFILE")
            if side_rooms[0] and side_rooms[1] and exterior[0] != exterior[1]:
                side_rooms = (
                    () if exterior[0] else side_rooms[0],
                    () if exterior[1] else side_rooms[1],
                )
            if not side_rooms[0] and not side_rooms[1]:
                reasons.append("CV_WINDOW_WALL_LINES_NO_ADJACENT_ROOM")
            elif set(side_rooms[0]) & set(side_rooms[1]):
                reasons.append("CV_WINDOW_WALL_LINES_SAME_ROOM_BOTH_SIDES")
            elif side_rooms[0] and side_rooms[1]:
                reasons.append("CV_WINDOW_WALL_LINES_BETWEEN_INTERIOR_ROOMS")
            if reasons:
                rejected.append(FusedOpening(
                    identifier, "window", segment.id, start, end,
                    width_mm, "rejected", tuple(reasons), side_rooms,
                ))
                reason_codes.extend(reasons)
                continue
            wall_candidates.append((
                segment, start, end, width_mm, side_rooms,
                width_mm < WINDOW_WIDTH_MIN_MM,
            ))

    kept = []
    segments_by_id = {item.id: item for item in graph.segments}
    for candidate in sorted(wall_candidates, key=lambda item: -item[3]):
        segment, start, end, _, _, _ = candidate
        overlapping_confirmed = [
            item for item in confirmed
            if (
            item.type == "window"
            and (existing_segment := segments_by_id.get(item.wall_edge_id)) is not None
            and segment.axis == existing_segment.axis
            and _parallel_interval_overlap(
                start, end, item.start_px, item.end_px, graph.wall_width_px
            )
            )
        ]
        if overlapping_confirmed:
            candidate_length = hypot(end[0] - start[0], end[1] - start[1])
            existing = overlapping_confirmed[0]
            existing_length = hypot(
                existing.end_px[0] - existing.start_px[0],
                existing.end_px[1] - existing.start_px[1],
            )
            if (
                len(overlapping_confirmed) == 1
                and "CV_WINDOW_AUTOMATIC" in existing.reason_codes
                and candidate_length >= existing_length * 1.25
            ):
                confirmed.remove(existing)
            else:
                continue
        if any(
            segment.axis == existing[0].axis
            and _parallel_interval_overlap(start, end, existing[1], existing[2], graph.wall_width_px)
            for existing in kept
        ):
            continue
        kept.append(candidate)
    for index, (segment, start, end, width_mm, side_rooms, bootstrap_tolerance) in enumerate(kept):
        confirmation_codes = ["CV_WINDOW_WALL_LINES_AUTOMATIC"]
        if bootstrap_tolerance:
            confirmation_codes.append("CV_WINDOW_BOOTSTRAP_SCALE_TOLERANCE")
        confirmed.append(FusedOpening(
            f"cv_window_wall_{index:03d}", "window", segment.id, start, end,
            width_mm, "confirmed", tuple(confirmation_codes), side_rooms,
        ))

    return OpeningsFusionResult(
        confirmed=tuple(confirmed), candidates=(), rejected=tuple(rejected),
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def _interval_overlap_length(a1, a2, b1, b2) -> float:
    axis = 0 if abs(a2[0] - a1[0]) >= abs(a2[1] - a1[1]) else 1
    left, right = sorted((a1[axis], a2[axis])), sorted((b1[axis], b2[axis]))
    return max(0.0, min(left[1], right[1]) - max(left[0], right[0]))


def _exterior_components(mask):
    if mask is None:
        return None, set()
    import cv2
    import numpy as np

    barrier = cv2.dilate((mask > 0).astype(np.uint8), np.ones((3, 3), np.uint8))
    _, labels = cv2.connectedComponents((barrier == 0).astype(np.uint8), connectivity=4)
    exterior = set(labels[0, :]) | set(labels[-1, :]) | set(labels[:, 0]) | set(labels[:, -1])
    exterior.discard(0)
    return labels, exterior


def _pixel_exterior_sides(labels, exterior_ids, start, end, wall_width_px):
    if labels is None:
        return False, False
    length = hypot(end[0] - start[0], end[1] - start[1])
    if length <= 0:
        return False, False
    midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    normal = (-(end[1] - start[1]) / length, (end[0] - start[0]) / length)
    offset = max(8.0, wall_width_px * 3.0)
    result = []
    for sign in (1, -1):
        x = min(labels.shape[1] - 1, max(0, round(midpoint[0] + normal[0] * offset * sign)))
        y = min(labels.shape[0] - 1, max(0, round(midpoint[1] + normal[1] * offset * sign)))
        result.append(int(labels[y, x]) in exterior_ids)
    return tuple(result)


def _window_intervals_on_wall(mask, segment, graph: WallGraph):
    """Find long two-line intervals inside an already-localized wall strip."""

    if mask is None or graph.mm_per_px <= 0:
        return ()
    import cv2
    import numpy as np

    height, width = mask.shape[:2]
    margin = max(4, int(round(graph.wall_width_px * 1.3)))
    if segment.axis == "horizontal":
        first = max(0, round(min(segment.start[0], segment.end[0])))
        last = min(width - 1, round(max(segment.start[0], segment.end[0])))
        fixed = round(segment.start[1])
        crop = mask[max(0, fixed - margin):min(height, fixed + margin + 1), first:last + 1]
    else:
        first = max(0, round(min(segment.start[1], segment.end[1])))
        last = min(height - 1, round(max(segment.start[1], segment.end[1])))
        fixed = round(segment.start[0])
        crop = mask[first:last + 1, max(0, fixed - margin):min(width, fixed + margin + 1)].T
    if crop.size == 0:
        return ()

    separated = np.zeros(crop.shape[1], np.uint8)
    for index in range(crop.shape[1]):
        ink = np.flatnonzero(crop[:, index])
        if (
            ink.size >= 2
            and 1 + np.count_nonzero(np.diff(ink) > 1) >= 2
            and ink[-1] - ink[0] + 1 <= graph.wall_width_px * 1.5
        ):
            separated[index] = 1
    separated = cv2.morphologyEx(
        separated[None, :], cv2.MORPH_CLOSE, np.ones((1, 5), np.uint8)
    )[0]
    minimum_px = max(4, round(WINDOW_WIDTH_MIN_MM / graph.mm_per_px * 0.7))
    intervals = []
    indices = np.flatnonzero(separated)
    if not indices.size:
        return ()
    start = previous = int(indices[0])
    for value in (*map(int, indices[1:]), None):
        if value is None or value - previous > 1:
            if previous - start + 1 >= minimum_px:
                low, high = first + start, first + previous
                if segment.axis == "horizontal":
                    intervals.append(((low, float(segment.start[1])), (high, float(segment.start[1]))))
                else:
                    intervals.append(((float(segment.start[0]), low), (float(segment.start[0]), high)))
            if value is None:
                break
            start = value
        previous = value
    return tuple(intervals)


def _point_segment_distance(point, start, end) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 0:
        return hypot(point[0] - start[0], point[1] - start[1])
    along = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared))
    return hypot(point[0] - start[0] - along * dx, point[1] - start[1] - along * dy)


def _door_arc_occupies_window_interval(arc, start, end, wall_width_px: float) -> bool:
    """A door vetoes panes only when its hinge lies on this wall interval."""

    horizontal = abs(end[0] - start[0]) >= abs(end[1] - start[1])
    axis, perpendicular = (0, 1) if horizontal else (1, 0)
    center = tuple(float(value) for value in arc.center)
    fixed = (start[perpendicular] + end[perpendicular]) / 2
    tolerance = max(8.0, wall_width_px * 1.5)
    low, high = sorted((start[axis], end[axis]))
    return (
        abs(center[perpendicular] - fixed) <= tolerance
        and low - tolerance <= center[axis] <= high + tolerance
    )


def _parallel_interval_overlap(a1, a2, b1, b2, wall_width_px: float) -> bool:
    horizontal = abs(a2[0] - a1[0]) >= abs(a2[1] - a1[1])
    axis = 0 if horizontal else 1
    perpendicular = 1 - axis
    if abs(a1[perpendicular] - b1[perpendicular]) > wall_width_px * 1.5:
        return False
    left = sorted((a1[axis], a2[axis]))
    right = sorted((b1[axis], b2[axis]))
    overlap = max(0.0, min(left[1], right[1]) - max(left[0], right[0]))
    return overlap >= min(left[1] - left[0], right[1] - right[0]) * 0.5


def arbitrate_cv_door_arcs(
    proposals,
    graph: WallGraph,
    rooms,
    image_width: int,
    image_height: int,
    *,
    wall_mask=None,
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
        base_angle = getattr(proposal, "base_angle", None)
        expected_axis = (
            "horizontal" if base_angle in (0, 180)
            else "vertical" if base_angle in (90, 270)
            else None
        )
        projection_limit = max(12.0, graph.wall_width_px * 2.5)
        nearby_gaps = [
            segment for segment in graph.segments
            if segment.virtual and _project_to_segment(center, segment)["distance"] <= projection_limit
        ]
        if nearby_gaps:
            expected_axis = min(
                nearby_gaps, key=lambda segment: _project_to_segment(center, segment)["distance"]
            ).axis
        elif not any(
            segment.axis == expected_axis and (
                _project_to_segment(center, segment)["distance"] <= projection_limit
                or _extend_arc_endpoint_carrier(
                    proposal, segment, projection_limit, graph.wall_width_px, expected_axis
                ) is not None
            )
            for segment in graph.segments
        ):
            expected_axis = _strong_wall_mask_axis(proposal, graph, wall_mask) or expected_axis
        if proposal.arc_score < min_arc_score:
            rejected.append(FusedOpening(
                opening_id, "door", "", center, center, None, "rejected",
                ("CV_ARC_SCORE_LOW",),
            ))
            reason_codes.append("CV_ARC_SCORE_LOW")
            continue

        nearest = []
        endpoint_carriers = []
        for segment in graph.segments:
            if expected_axis is not None and segment.axis != expected_axis:
                continue
            projected = _project_to_segment(center, segment)
            endpoint_extended = False
            extended = _extend_arc_endpoint_carrier(
                proposal, segment, projection_limit, graph.wall_width_px, expected_axis
            )
            if extended is not None:
                segment = extended
                projected = _project_to_segment(center, segment)
                endpoint_extended = True
            elif projected["distance"] > projection_limit:
                continue
            if projected["distance"] <= projection_limit:
                segment_length = hypot(
                    segment.end[0] - segment.start[0], segment.end[1] - segment.start[1]
                )
                segment_unit = (
                    (segment.end[0] - segment.start[0]) / segment_length,
                    (segment.end[1] - segment.start[1]) / segment_length,
                ) if segment_length > 0 else (0.0, 0.0)
                sign = _arc_wall_sign(proposal, segment, segment_unit, require_opt_in=True)
                available = (
                    segment_length - projected["along_span"] if sign == 1
                    else projected["along_span"] if sign == -1
                    else segment_length
                )
                short_in_arc_direction = available < float(proposal.radius_px) * 0.7
                candidate = (
                    short_in_arc_direction, 0 if segment.virtual else 1,
                    projected["distance"], segment, projected, endpoint_extended,
                )
                (endpoint_carriers if endpoint_extended else nearest).append(candidate)
        if not nearest:
            nearest = endpoint_carriers
        if not nearest:
            rejected.append(FusedOpening(
                opening_id, "door", "", center, center, None, "rejected",
                ("CV_ARC_NO_WALL_EDGE",),
            ))
            reason_codes.append("CV_ARC_NO_WALL_EDGE")
            continue

        _, _, _, segment, projected, endpoint_extended = min(nearest, key=lambda item: item[:3])
        length = hypot(segment.end[0] - segment.start[0], segment.end[1] - segment.start[1])
        if length <= 0:
            continue
        unit = ((segment.end[0] - segment.start[0]) / length, (segment.end[1] - segment.start[1]) / length)
        subdivided_gap = False
        bootstrap_minimum_width = max(
            0.0, DOOR_WIDTH_MIN_MM * 0.7 - max(0.0, graph.mm_per_px)
        )
        if segment.virtual:
            radius = max(1.0, float(proposal.radius_px))
            full_width_mm = length * graph.mm_per_px if graph.mm_per_px > 0 else None
            if (
                full_width_mm is not None
                and bootstrap_minimum_width <= full_width_mm <= DOOR_WIDTH_MAX_MM
                and (
                    length <= radius * 2.0
                    or radius * graph.mm_per_px < bootstrap_minimum_width
                )
            ):
                start_px, end_px = segment.start, segment.end
                width_px = length
            else:
                # One graph gap can span two adjacent door leaves when the
                # narrow wall post between them was lost. Use each arc radius
                # as its own interval instead of treating the entire gap as one
                # oversized door.
                width_px = min(length, radius)
                sign = _arc_wall_sign(proposal, segment, unit)
                if sign == 1:
                    start_along = projected["along_span"]
                    end_along = min(length, start_along + width_px)
                elif sign == -1:
                    end_along = projected["along_span"]
                    start_along = max(0.0, end_along - width_px)
                else:
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
            radius = max(1.0, float(proposal.radius_px))
            sign = _arc_wall_sign(proposal, segment, unit)
            if sign is None:
                start_along = max(0.0, projected["along_span"] - radius)
                end_along = min(length, projected["along_span"] + radius)
            else:
                start_along, end_along = sorted((
                    projected["along_span"], projected["along_span"] + sign * radius,
                ))
                start_along, end_along = max(0.0, start_along), min(length, end_along)
            width_px = end_along - start_along
            start_px = (segment.start[0] + unit[0] * start_along, segment.start[1] + unit[1] * start_along)
            end_px = (segment.start[0] + unit[0] * end_along, segment.start[1] + unit[1] * end_along)

        width_mm = width_px * graph.mm_per_px if graph.mm_per_px > 0 else None
        reasons: list[str] = []
        # Wall-thickness scale is only a bootstrap estimate. Keep the physical
        # door profile, but tolerate its known under-scaling plus one raster
        # pixel when a strong arc is anchored to a wall and passes topology.
        minimum_width = max(
            0.0,
            DOOR_WIDTH_MIN_MM * (0.5 if proposal.arc_score >= 0.96 else 0.7)
            - max(0.0, graph.mm_per_px),
        )
        if width_mm is None or not minimum_width <= width_mm <= DOOR_WIDTH_MAX_MM:
            reasons.append("CV_ARC_WIDTH_OUT_OF_PROFILE")
        if not segment.virtual and proposal.arc_score < 0.96:
            reasons.append("CV_ARC_SOLID_WALL_SCORE_LOW")
        side_rooms = _room_sides_for_segment(start_px, end_px, segment, room_polygons, graph.wall_width_px)
        if not side_rooms[0] and not side_rooms[1]:
            reasons.append("CV_ARC_NO_ADJACENT_ROOM")
        elif set(side_rooms[0]) & set(side_rooms[1]) and not (
            proposal.arc_score >= 0.96 and (
                segment.virtual or endpoint_extended or segment.id.startswith("wall_arc_carrier_")
            )
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
        if segment.id.startswith("wall_arc_carrier_"):
            confirmation_codes.append("CV_ARC_WALL_MASK_CARRIER")
        if endpoint_extended:
            confirmation_codes.append("CV_ARC_ENDPOINT_CARRIER_EXTENDED")
        if width_mm is not None and width_mm < DOOR_WIDTH_MIN_MM:
            confirmation_codes.append("CV_ARC_BOOTSTRAP_SCALE_TOLERANCE")
        if set(side_rooms[0]) & set(side_rooms[1]):
            confirmation_codes.append(
                "CV_ARC_SHARED_ROOM_PHYSICAL_GAP" if segment.virtual
                else "CV_ARC_SHARED_ROOM_RECOVERED_CARRIER"
            )
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


def recover_cv_door_carriers(proposals, graph: WallGraph, wall_mask) -> WallGraph:
    """Recover short carrier edges when a door symbol hid the wall vector."""

    if wall_mask is None:
        return graph
    import numpy as np

    recovered = []
    for index, proposal in enumerate(proposals):
        center = tuple(float(value) for value in proposal.center)
        projection_limit = max(12.0, graph.wall_width_px * 2.5)
        base_angle = getattr(proposal, "base_angle", None)
        expected_axis = (
            "horizontal" if base_angle in (0, 180)
            else "vertical" if base_angle in (90, 270)
            else None
        )
        nearby_gaps = [
            segment for segment in graph.segments
            if segment.virtual and _project_to_segment(center, segment)["distance"] <= projection_limit
        ]
        if nearby_gaps:
            expected_axis = min(
                nearby_gaps, key=lambda segment: _project_to_segment(center, segment)["distance"]
            ).axis
        elif not any(
            segment.axis == expected_axis and (
                _project_to_segment(center, segment)["distance"] <= projection_limit
                or _extend_arc_endpoint_carrier(
                    proposal, segment, projection_limit, graph.wall_width_px, expected_axis
                ) is not None
            )
            for segment in graph.segments
        ):
            expected_axis = _strong_wall_mask_axis(proposal, graph, wall_mask) or expected_axis
        if any(
            segment.axis == expected_axis and (
                _project_to_segment(center, segment)["distance"] <= projection_limit
                or _extend_arc_endpoint_carrier(
                    proposal, segment, projection_limit, graph.wall_width_px, expected_axis
                ) is not None
            )
            for segment in graph.segments
        ):
            continue

        horizontal = expected_axis == "horizontal"
        if expected_axis not in ("horizontal", "vertical"):
            continue
        radius = float(proposal.radius_px)
        span = max(4, round(radius * 1.35))
        cx, cy = center
        if horizontal:
            x1, x2 = max(0, round(cx - span)), min(wall_mask.shape[1], round(cx + span) + 1)
            y1, y2 = max(0, round(cy - projection_limit)), min(wall_mask.shape[0], round(cy + projection_limit) + 1)
            scores = wall_mask[y1:y2, x1:x2].mean(axis=1)
        else:
            y1, y2 = max(0, round(cy - span)), min(wall_mask.shape[0], round(cy + span) + 1)
            x1, x2 = max(0, round(cx - projection_limit)), min(wall_mask.shape[1], round(cx + projection_limit) + 1)
            scores = wall_mask[y1:y2, x1:x2].mean(axis=0)
        if not scores.size:
            continue
        best = int(np.argmax(scores))
        best_score = float(scores[best])
        axis = float((y1 if horizontal else x1) + best)
        minimum_coverage = (
            0.15 if proposal.arc_score >= 0.96 and radius >= graph.wall_width_px * 2.75
            else 0.35
        )
        if best_score < minimum_coverage or abs(axis - (cy if horizontal else cx)) > max(3.0, graph.wall_width_px):
            continue

        sign = 1 if base_angle in (0, 90) else -1
        if horizontal:
            start, end = (cx, axis), (cx + sign * radius, axis)
            wall_axis = "horizontal"
        else:
            start, end = (axis, cy), (axis, cy + sign * radius)
            wall_axis = "vertical"
        start, end = sorted((start, end), key=lambda point: point[0] if horizontal else point[1])
        recovered.append(WallSegment(
            f"wall_arc_carrier_{index:03d}", wall_axis, start, end,
            graph.wall_width_px, False,
        ))

    return replace(graph, segments=(*graph.segments, *recovered))


def _strong_wall_mask_axis(proposal, graph: WallGraph, wall_mask) -> str | None:
    # Thick morphology absorbs the door leaf into the wall layer, so its
    # dominant direction is not independent carrier evidence.
    if wall_mask is None or graph.wall_width_px > 8.0:
        return None
    radius = float(proposal.radius_px)
    cx, cy = proposal.center
    span = max(4, round(radius * 1.35))
    band = max(3, round(max(12.0, graph.wall_width_px * 2.5)))
    horizontal = wall_mask[
        max(0, round(cy - band)):min(wall_mask.shape[0], round(cy + band) + 1),
        max(0, round(cx - span)):min(wall_mask.shape[1], round(cx + span) + 1),
    ]
    vertical = wall_mask[
        max(0, round(cy - span)):min(wall_mask.shape[0], round(cy + span) + 1),
        max(0, round(cx - band)):min(wall_mask.shape[1], round(cx + band) + 1),
    ]
    horizontal_score = float(horizontal.mean(axis=1).max()) if horizontal.size else 0.0
    vertical_score = float(vertical.mean(axis=0).max()) if vertical.size else 0.0
    if horizontal_score >= max(0.35, vertical_score * 1.5):
        return "horizontal"
    if vertical_score >= max(0.35, horizontal_score * 1.5):
        return "vertical"
    return None


def _extend_arc_endpoint_carrier(
    proposal, segment, projection_limit: float, wall_width_px: float,
    expected_axis: str | None = None,
):
    """Bridge a door-sized wall-end gap hidden by the detected door symbol."""

    base_angle = getattr(proposal, "base_angle", None)
    expected_axis = expected_axis or (
        "horizontal" if base_angle in (0, 180)
        else "vertical" if base_angle in (90, 270)
        else None
    )
    if segment.virtual or segment.axis != expected_axis:
        return None
    coordinate = 0 if segment.axis == "horizontal" else 1
    perpendicular = 1 - coordinate
    center = tuple(float(value) for value in proposal.center)
    if abs(center[perpendicular] - segment.start[perpendicular]) > projection_limit:
        return None
    low, high = sorted((segment.start[coordinate], segment.end[coordinate]))
    gap = max(low - center[coordinate], center[coordinate] - high, 0.0)
    radius = float(proposal.radius_px)
    if gap > radius + max(2.0, wall_width_px * 0.5):
        return None

    length = hypot(segment.end[0] - segment.start[0], segment.end[1] - segment.start[1])
    if length <= 0:
        return None
    unit = ((segment.end[0] - segment.start[0]) / length, (segment.end[1] - segment.start[1]) / length)
    sign = _arc_wall_sign(proposal, segment, unit)
    if sign is None:
        return None
    target = center[coordinate] + sign * radius * unit[coordinate]
    if gap == 0 and low <= target <= high:
        return None
    low, high = min(low, center[coordinate], target), max(high, center[coordinate], target)
    if segment.axis == "horizontal":
        return replace(segment, start=(low, segment.start[1]), end=(high, segment.start[1]))
    return replace(segment, start=(segment.start[0], low), end=(segment.start[0], high))


def _arc_wall_sign(proposal, segment, unit, *, require_opt_in: bool = False) -> int | None:
    """Map the detected quarter-arc quadrant to its one-sided wall direction."""

    base_angle = getattr(proposal, "base_angle", None)
    if base_angle is None or (
        require_opt_in and not getattr(proposal, "use_one_sided_interval", False)
    ):
        return None
    if segment.axis == "horizontal":
        direction = (1.0 if base_angle in (0, 270) else -1.0, 0.0)
    else:
        direction = (0.0, 1.0 if base_angle in (0, 90) else -1.0)
    return 1 if direction[0] * unit[0] + direction[1] * unit[1] >= 0 else -1


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


def _room_sides_for_segment(
    start, end, segment, room_polygons, wall_width_px: float,
    sample_offset_factor: float = 1.5,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Sample room polygons on both sides of a pixel wall segment."""

    length = hypot(end[0] - start[0], end[1] - start[1])
    if length <= 0:
        return (), ()
    midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    normal = (-(end[1] - start[1]) / length, (end[0] - start[0]) / length)
    offset = max(8.0, wall_width_px * sample_offset_factor)
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
