from __future__ import annotations

from dataclasses import dataclass
from math import ceil, cos, floor, hypot, radians, sin

import cv2
import numpy as np


@dataclass(frozen=True)
class OpeningDetectionProfile:
    wall_short_edge_min_mm: float = 80
    wall_short_edge_max_mm: float = 450
    door_width_min_mm: float = 500
    door_width_max_mm: float = 1500
    single_door_width_max_mm: float = 1100
    opposite_overlap_min_mm: float = 50
    window_line_cluster_mm: float = 350
    same_direction_dot: float = 0.99


DEFAULT_OPENING_PROFILE = OpeningDetectionProfile()


@dataclass(frozen=True)
class PixelWallSegment:
    id: str
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass(frozen=True)
class WallAnchoredDoorArc:
    wall_edge_id: str
    pixel_start: tuple[float, float]
    pixel_end: tuple[float, float]
    anchor: tuple[float, float]
    radius_px: float
    arc_score: float


@dataclass(frozen=True)
class DoorArcProposal:
    center: tuple[float, float]
    radius_px: float
    arc_score: float


@dataclass(frozen=True)
class Line2D:
    start: tuple[float, float]
    end: tuple[float, float]

    @property
    def length(self) -> float:
        return hypot(self.end[0] - self.start[0], self.end[1] - self.start[1])

    @property
    def unit(self) -> tuple[float, float]:
        length = self.length
        if length == 0:
            raise ValueError("line must have non-zero length")
        return ((self.end[0] - self.start[0]) / length, (self.end[1] - self.start[1]) / length)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.start[0] + self.end[0]) / 2, (self.start[1] + self.end[1]) / 2)


def is_door_rect_pair(
    short_edge_length_mm: float,
    opposite_distance_mm: float,
    overlap_length_mm: float,
    normals_opposite: bool,
    profile: OpeningDetectionProfile = DEFAULT_OPENING_PROFILE,
) -> bool:
    return (
        profile.wall_short_edge_min_mm < short_edge_length_mm < profile.wall_short_edge_max_mm
        and opposite_distance_mm >= profile.door_width_min_mm
        and overlap_length_mm >= profile.opposite_overlap_min_mm
        and normals_opposite
    )


def choose_single_door_origin(
    entity_points: list[tuple[float, float]],
    rectangle_vertices: list[tuple[float, float]],
) -> tuple[float, float]:
    """Choose the bounding-box vertex with the lowest distance dispersion."""
    if not entity_points or not rectangle_vertices:
        raise ValueError("entity_points and rectangle_vertices are required")
    best_vertex = rectangle_vertices[0]
    best_dispersion = float("inf")
    for vertex in rectangle_vertices:
        distances = [hypot(point[0] - vertex[0], point[1] - vertex[1]) for point in entity_points]
        mean_distance = sum(distances) / len(distances)
        dispersion = sum(abs(distance - mean_distance) for distance in distances)
        if dispersion < best_dispersion:
            best_vertex = vertex
            best_dispersion = dispersion
    return best_vertex


def cluster_collinear_window_lines(
    lines: list[Line2D],
    profile: OpeningDetectionProfile = DEFAULT_OPENING_PROFILE,
) -> list[list[Line2D]]:
    remaining = sorted(lines, key=lambda line: line.length, reverse=True)
    clusters: list[list[Line2D]] = []
    while remaining:
        seed = remaining.pop(0)
        seed_unit = seed.unit
        seed_center = seed.center
        cluster = [seed]
        unmatched = []
        for candidate in remaining:
            candidate_unit = candidate.unit
            direction_dot = abs(seed_unit[0] * candidate_unit[0] + seed_unit[1] * candidate_unit[1])
            normal = (-seed_unit[1], seed_unit[0])
            center_delta = (
                candidate.center[0] - seed_center[0],
                candidate.center[1] - seed_center[1],
            )
            perpendicular_distance = abs(center_delta[0] * normal[0] + center_delta[1] * normal[1])
            if (
                direction_dot >= profile.same_direction_dot
                and perpendicular_distance <= profile.window_line_cluster_mm
            ):
                cluster.append(candidate)
            else:
                unmatched.append(candidate)
        clusters.append(cluster)
        remaining = unmatched
    return clusters


def detect_door_arc_proposals(grayscale: np.ndarray) -> list[DoorArcProposal]:
    """Propose door arcs near long dark wall strokes; topology validation remains mandatory."""
    short_edge = min(grayscale.shape[:2])
    scale = short_edge / 800
    min_radius = max(4, round(8 * scale))
    max_radius = max(min_radius + 2, round(50 * scale))
    circles = cv2.HoughCircles(
        grayscale,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=max(6, round(10 * scale)),
        param1=100,
        param2=20,
        minRadius=min_radius,
        maxRadius=max_radius,
    )
    if circles is None:
        return []

    dark = (grayscale < 80).astype(np.uint8)
    thickness = max(2, round(2 * scale))
    length = max(8, round(12 * scale))
    horizontal = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((thickness, length), np.uint8))
    vertical = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((length, thickness), np.uint8))
    wall_distance = cv2.distanceTransform(1 - ((horizontal | vertical) > 0).astype(np.uint8), cv2.DIST_L2, 3)
    maximum_wall_distance = max(3, round(9 * scale))
    proposals = []
    for center_x, center_y, radius in circles[0]:
        score, _ = _best_quarter_arc(grayscale, (center_x, center_y), radius)
        x = min(grayscale.shape[1] - 1, max(0, round(center_x)))
        y = min(grayscale.shape[0] - 1, max(0, round(center_y)))
        if score >= 0.88 and wall_distance[y, x] <= maximum_wall_distance:
            if _is_closed_map_marker(grayscale, (center_x, center_y), radius, scale):
                # Floorplan north arrows/compasses are closed rings, unlike a
                # door leaf's single quarter arc. They commonly sit in the
                # drawing margin and must never become opening proposals.
                continue
            proposals.append(DoorArcProposal(
                center=(float(center_x), float(center_y)),
                radius_px=float(radius),
                arc_score=score,
            ))
    return _deduplicate_proposals(proposals, scale)


def _is_closed_map_marker(
    grayscale: np.ndarray, center: tuple[float, float], radius: float, scale: float
) -> bool:
    """Reject small closed circular legend markers such as the north compass."""

    if radius > max(30.0, 0.05 * min(grayscale.shape[:2])):
        return False
    hits = 0
    samples = 72
    for angle in np.linspace(0, 360, samples, endpoint=False):
        radians = np.deg2rad(angle)
        x = round(center[0] + radius * np.cos(radians))
        y = round(center[1] + radius * np.sin(radians))
        patch = grayscale[max(0, y - 2):y + 3, max(0, x - 2):x + 3]
        hits += bool(patch.size and patch.min() < 110)
    coverage = hits / samples
    if coverage >= 0.82:
        return True
    # Some compass drawings contain a broken inner ring rather than a fully
    # closed outer circle. A small circular mark in the lower-left map margin
    # is still a legend symbol, while real door arcs are anchored in the plan.
    in_lower_left_margin = (
        center[0] <= grayscale.shape[1] * 0.28
        and center[1] >= grayscale.shape[0] * 0.76
    )
    return in_lower_left_margin and coverage >= 0.55


def _deduplicate_proposals(proposals: list[DoorArcProposal], scale: float) -> list[DoorArcProposal]:
    kept = []
    for candidate in sorted(proposals, key=lambda item: -item.arc_score):
        duplicate = any(
            hypot(candidate.center[0] - existing.center[0], candidate.center[1] - existing.center[1])
            <= max(4, 0.5 * min(candidate.radius_px, existing.radius_px))
            and max(candidate.radius_px, existing.radius_px) / min(candidate.radius_px, existing.radius_px) < 1.7
            for existing in kept
        )
        if not duplicate:
            kept.append(candidate)
    return kept


def detect_wall_anchored_door_arcs(
    grayscale: np.ndarray,
    walls: list[PixelWallSegment],
    mm_per_pixel: float,
    profile: OpeningDetectionProfile = DEFAULT_OPENING_PROFILE,
) -> list[WallAnchoredDoorArc]:
    circles = cv2.HoughCircles(
        grayscale,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=max(8, round(profile.door_width_min_mm / mm_per_pixel / 2)),
        param1=100,
        param2=18,
        minRadius=ceil(profile.door_width_min_mm / mm_per_pixel),
        maxRadius=floor(profile.single_door_width_max_mm / mm_per_pixel),
    )
    if circles is None:
        return []

    candidates = []
    for center_x, center_y, radius in circles[0]:
        projections = [(_project_to_segment((center_x, center_y), wall), wall) for wall in walls]
        (distance, along, anchor), wall = min(projections, key=lambda item: item[0][0])
        if distance > 4 or not 0.05 < along < 0.95:
            continue
        score, base_angle = _best_quarter_arc(grayscale, anchor, radius)
        if score < 0.75:
            continue
        unit = _unit(wall.start, wall.end)
        arc_direction = (cos(radians(base_angle + 45)), sin(radians(base_angle + 45)))
        sign = 1 if arc_direction[0] * unit[0] + arc_direction[1] * unit[1] >= 0 else -1
        wall_length = hypot(wall.end[0] - wall.start[0], wall.end[1] - wall.start[1])
        anchor_offset = along * wall_length
        first, second = sorted((anchor_offset, anchor_offset + sign * radius))
        first, second = max(0, first), min(wall_length, second)
        if (second - first) * mm_per_pixel < profile.door_width_min_mm:
            continue
        candidates.append(WallAnchoredDoorArc(
            wall_edge_id=wall.id,
            pixel_start=(wall.start[0] + unit[0] * first, wall.start[1] + unit[1] * first),
            pixel_end=(wall.start[0] + unit[0] * second, wall.start[1] + unit[1] * second),
            anchor=anchor,
            radius_px=float(radius),
            arc_score=score,
        ))
    return _deduplicate_arcs(candidates)


def _project_to_segment(point: tuple[float, float], wall: PixelWallSegment):
    dx, dy = wall.end[0] - wall.start[0], wall.end[1] - wall.start[1]
    length_squared = dx * dx + dy * dy
    along = max(0.0, min(1.0, ((point[0] - wall.start[0]) * dx + (point[1] - wall.start[1]) * dy) / length_squared))
    anchor = (wall.start[0] + along * dx, wall.start[1] + along * dy)
    return hypot(point[0] - anchor[0], point[1] - anchor[1]), along, anchor


def _unit(start: tuple[float, float], end: tuple[float, float]):
    length = hypot(end[0] - start[0], end[1] - start[1])
    return (end[0] - start[0]) / length, (end[1] - start[1]) / length


def _best_quarter_arc(grayscale: np.ndarray, center: tuple[float, float], radius: float):
    best = (0.0, 0)
    for base_angle in (0, 90, 180, 270):
        hits = 0
        for angle in np.linspace(5, 85, 25):
            x = round(center[0] + radius * cos(radians(base_angle + angle)))
            y = round(center[1] + radius * sin(radians(base_angle + angle)))
            patch = grayscale[max(0, y - 2): y + 3, max(0, x - 2): x + 3]
            hits += bool(patch.size and patch.min() < 110)
        score = hits / 25
        if score > best[0]:
            best = score, base_angle
    return best


def _deduplicate_arcs(candidates: list[WallAnchoredDoorArc]):
    kept = []
    for candidate in sorted(candidates, key=lambda item: (-item.arc_score, item.radius_px)):
        duplicate = any(
            candidate.wall_edge_id == existing.wall_edge_id and _interval_iou(candidate, existing) >= 0.4
            for existing in kept
        )
        if not duplicate:
            kept.append(candidate)
    return kept


def _interval_iou(left: WallAnchoredDoorArc, right: WallAnchoredDoorArc):
    axis = 0 if abs(left.pixel_end[0] - left.pixel_start[0]) >= abs(left.pixel_end[1] - left.pixel_start[1]) else 1
    left_interval = sorted((left.pixel_start[axis], left.pixel_end[axis]))
    right_interval = sorted((right.pixel_start[axis], right.pixel_end[axis]))
    intersection = max(0, min(left_interval[1], right_interval[1]) - max(left_interval[0], right_interval[0]))
    union = max(left_interval[1], right_interval[1]) - min(left_interval[0], right_interval[0])
    return intersection / union if union else 0
