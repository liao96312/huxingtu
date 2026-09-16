from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import LineString, Point
from shapely.ops import polygonize, unary_union

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pano_platform.contracts import (  # noqa: E402
    DerivedRoomSet,
    LayoutRevision,
    OpeningCandidateSet,
    Point2D,
    ScaleCalibrationRequest,
    canonical_sha256,
)
from pano_platform.geometry import calibrate_scale, transform_point  # noqa: E402
from pano_platform.opening_detection import DEFAULT_OPENING_PROFILE  # noqa: E402

SOURCE = ROOT / "datasets/source/fp-001.png"
CALIBRATION = ROOT / "tests/fixtures/scale-calibration.fp-001.json"
OUTPUT_ROOT = ROOT / "datasets/derived/fp-001"
DEBUG_ROOT = ROOT / "datasets/debug"

# Manually corrected wall centerlines after Hough candidate inspection.
SEGMENTS = [
    ("w_outer_top_left", (184, 154), (302, 154), 180, 0.90),
    ("w_outer_step_1", (302, 154), (302, 137), 180, 0.85),
    ("w_outer_center_recess", (302, 137), (282, 137), 180, 0.82),
    ("w_outer_bump_left", (282, 137), (282, 118), 180, 0.82),
    ("w_outer_bump_top", (282, 118), (345, 118), 180, 0.88),
    ("w_outer_bump_right", (345, 118), (345, 133), 180, 0.82),
    ("w_outer_top_center", (345, 133), (379, 133), 180, 0.86),
    ("w_outer_step_2", (379, 133), (379, 172), 180, 0.85),
    ("w_outer_top_bath", (379, 172), (433, 172), 180, 0.86),
    ("w_outer_step_3", (433, 172), (433, 201), 180, 0.82),
    ("w_outer_top_kitchen", (433, 201), (494, 201), 180, 0.88),
    ("w_outer_right_kitchen", (494, 201), (494, 321), 180, 0.90),
    ("w_outer_bottom_kitchen", (494, 321), (433, 321), 180, 0.88),
    ("w_outer_right_living", (433, 321), (433, 365), 180, 0.85),
    ("w_outer_bottom_living", (433, 365), (200, 365), 180, 0.88),
    ("w_outer_left_living", (200, 365), (200, 258), 180, 0.80),
    ("w_outer_recess", (200, 258), (184, 258), 180, 0.80),
    ("w_outer_left_bed", (184, 258), (184, 154), 180, 0.90),
    ("w_bed_left_right", (302, 154), (302, 258), 120, 0.86),
    ("w_bed_left_bottom", (200, 258), (302, 258), 120, 0.84),
    ("w_bed_center_right", (379, 172), (379, 258), 120, 0.82),
    ("w_bed_center_bottom", (302, 258), (379, 258), 120, 0.82),
    ("w_bath_bottom", (379, 258), (433, 258), 120, 0.78),
    ("w_kitchen_left_upper", (433, 201), (433, 258), 120, 0.80),
    ("w_kitchen_left_lower", (433, 258), (433, 321), 120, 0.78),
]

ROOMS = [
    ("room_bed_left", "卧室", "bedroom", (250, 205), 0.88),
    ("room_bed_center", "卧室", "bedroom", (340, 190), 0.84),
    ("room_bath", "卫生间", "bathroom", (405, 215), 0.80),
    ("room_kitchen", "厨房", "kitchen", (465, 260), 0.84),
    ("room_living_dining", "客餐厅", "living_dining", (330, 320), 0.86),
]


def main() -> None:
    calibration_request = ScaleCalibrationRequest.model_validate(
        json.loads(CALIBRATION.read_text(encoding="utf-8"))
    )
    calibration = calibrate_scale(calibration_request)
    scale = calibration.mmPerPixel
    matrix = calibration.pixelToWorldTransform

    points = sorted({point for _, start, end, _, _ in SEGMENTS for point in (start, end)})
    node_ids = {point: f"n{index:03d}" for index, point in enumerate(points, start=1)}
    nodes = []
    for point in points:
        world = transform_point(matrix, Point2D(x=point[0], y=point[1]))
        nodes.append({"id": node_ids[point], "x": round(world.x, 6), "y": round(world.y, 6)})

    edges = []
    segment_map = {}
    for identifier, start, end, thickness, confidence in SEGMENTS:
        edges.append(
            {
                "id": identifier,
                "startNodeId": node_ids[start],
                "endNodeId": node_ids[end],
                "thickness": thickness,
                "height": 2800,
                "confidence": confidence,
            }
        )
        segment_map[identifier] = (start, end)

    def edge_length(identifier: str) -> float:
        start, end = segment_map[identifier]
        return LineString([start, end]).length * scale

    # Low-confidence openings do not enter LayoutRevision until candidate review.
    openings = []

    room_annotations = []
    for identifier, name, room_type, anchor, confidence in ROOMS:
        world = transform_point(matrix, Point2D(x=anchor[0], y=anchor[1]))
        room_annotations.append(
            {
                "id": identifier,
                "type": room_type,
                "label": name,
                "anchor": {"x": round(world.x, 6), "y": round(world.y, 6)},
                "confidence": confidence,
            }
        )

    layout_payload = {
        "schemaVersion": "0.1.0",
        "id": "layout_fp001_manual_r2",
        "projectId": "fp001",
        "revision": 2,
        "units": "mm",
        "coordinateFrame": "business-z-up-rh",
        "source": {
            "imageArtifactId": "dataset:desktop-floorplans-poc:fp-001",
            "pixelToWorldTransform": list(matrix),
            "scaleConfidence": 0.90,
        },
        "nodes": nodes,
        "edges": edges,
        "openings": openings,
        "roomAnnotations": room_annotations,
    }
    layout_payload["contentHash"] = canonical_sha256(layout_payload)
    layout = LayoutRevision.model_validate(layout_payload)
    layout_document = layout.model_dump(mode="json", exclude_none=True)
    layout_document.pop("contentHash")
    layout_document["contentHash"] = canonical_sha256(layout_document)
    layout = LayoutRevision.model_validate(layout_document)

    linework = unary_union([LineString([start, end]) for _, start, end, _, _ in SEGMENTS])
    polygons = [polygon for polygon in polygonize(linework) if polygon.area > 100]
    derived_rooms = []
    room_polygons = {}
    matched_polygons = set()
    for identifier, name, room_type, anchor, confidence in ROOMS:
        matches = [polygon for polygon in polygons if polygon.covers(Point(anchor))]
        if len(matches) != 1:
            raise RuntimeError(f"room {identifier} matched {len(matches)} polygons")
        polygon = matches[0]
        room_polygons[identifier] = polygon
        matched_polygons.add(polygon.wkb_hex)
        coordinates = []
        for x, y in list(polygon.exterior.coords)[:-1]:
            world = transform_point(matrix, Point2D(x=x, y=y))
            coordinates.append({"x": round(world.x, 6), "y": round(world.y, 6)})
        derived_rooms.append(
            {
                "id": identifier,
                "name": name,
                "type": room_type,
                "polygon": coordinates,
                "areaMm2": round(polygon.area * scale * scale, 3),
                "confidence": confidence,
            }
        )
    if len(matched_polygons) != len(ROOMS):
        raise RuntimeError("room annotations did not map one-to-one to polygons")

    rooms_payload = {
        "schemaVersion": "0.1.0",
        "projectId": "fp001",
        "layoutRevisionId": layout.id,
        "sourceLayoutHash": layout.contentHash,
        "rooms": derived_rooms,
    }
    rooms_payload["contentHash"] = canonical_sha256(rooms_payload)
    rooms = DerivedRoomSet.model_validate(rooms_payload)

    detected_candidates = detect_opening_candidates(segment_map, room_polygons, scale)
    candidate_payload = {
        "schemaVersion": "0.1.0",
        "projectId": "fp001",
        "layoutRevisionId": layout.id,
        "sourceLayoutHash": layout.contentHash,
        "status": "needsReview",
        "candidates": detected_candidates,
    }
    candidate_payload["contentHash"] = canonical_sha256(candidate_payload)
    candidates = OpeningCandidateSet.model_validate(candidate_payload)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    DEBUG_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "layout-revision.manual-v2.json").write_text(
        json.dumps(layout.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (OUTPUT_ROOT / "rooms.manual-v2.json").write_text(
        json.dumps(rooms.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (OUTPUT_ROOT / "opening-candidates.auto-v2.json").write_text(
        json.dumps(candidates.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    candidate_documents = candidates.model_dump(mode="json")["candidates"]
    render_overlay(polygons, segment_map, candidate_documents, room_annotations, matrix)
    alignment = measure_wall_alignment(segment_map, candidate_documents, scale)
    quality = {
        "schemaVersion": "0.1.0",
        "layoutRevisionId": layout.id,
        "layoutHash": layout.contentHash,
        "sourceImage": "datasets/source/fp-001.png",
        "sourceShortEdge": 450,
        "wallAlignmentCoverage": alignment["coverage"],
        "wallAlignmentSamples": alignment["samples"],
        "perEdgeCoverage": alignment["perEdgeCoverage"],
        "scaleMaxRelativeResidual": calibration.maxRelativeResidual,
        "decision": "needsReview",
        "openingCandidateCount": len(candidates.candidates),
        "reasonCodes": ["SHORT_EDGE_BELOW_1200", "OPENING_CANDIDATES_REQUIRE_REVIEW"],
    }
    quality["contentHash"] = canonical_sha256(quality)
    (OUTPUT_ROOT / "quality.manual-v2.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "layoutHash": layout.contentHash,
                "roomSetHash": rooms.contentHash,
                "nodes": len(layout.nodes),
                "edges": len(layout.edges),
                "openings": len(layout.openings),
                "openingCandidates": len(candidates.candidates),
                "rooms": len(rooms.rooms),
                "areaMm2": round(sum(room.areaMm2 for room in rooms.rooms), 3),
                "wallAlignmentCoverage": alignment["coverage"],
            },
            indent=2,
        )
    )


def detect_opening_candidates(segment_map, room_polygons, scale: float) -> list[dict]:
    grayscale = cv2.imread(str(SOURCE), cv2.IMREAD_GRAYSCALE)
    profile = DEFAULT_OPENING_PROFILE

    def dark_near(x: float, y: float, radius: int = 2) -> bool:
        x_int, y_int = round(x), round(y)
        patch = grayscale[
            max(0, y_int - radius) : y_int + radius + 1,
            max(0, x_int - radius) : x_int + radius + 1,
        ]
        return bool(patch.size and patch.min() < 110)

    def best_quarter_arc(point: tuple[float, float]) -> tuple[float, int]:
        best_score, best_radius = 0.0, 0
        minimum_radius = max(4, round(profile.door_width_min_mm / scale))
        maximum_radius = max(minimum_radius + 2, round(profile.door_width_max_mm / scale))
        for radius in range(minimum_radius, maximum_radius + 1, 2):
            for base_angle in (0, 90, 180, 270):
                hits = 0
                for angle in np.linspace(5, 85, 25):
                    radians = math.radians(base_angle + angle)
                    hits += dark_near(
                        point[0] + radius * math.cos(radians),
                        point[1] + radius * math.sin(radians),
                    )
                score = hits / 25
                if score > best_score:
                    best_score, best_radius = score, radius
        return best_score, best_radius

    def room_at(point: tuple[float, float]) -> str | None:
        for room_id, polygon in room_polygons.items():
            if polygon.covers(Point(point)):
                return room_id
        return None

    detected = []
    for identifier, start, end, _, _ in SEGMENTS:
        pixel_length = math.dist(start, end)
        if pixel_length < 30:
            continue
        steps = max(2, round(pixel_length))
        unit_x = (end[0] - start[0]) / pixel_length
        unit_y = (end[1] - start[1]) / pixel_length
        normal_x, normal_y = -unit_y, unit_x
        strengths = []
        for step in range(steps + 1):
            x = start[0] + unit_x * step
            y = start[1] + unit_y * step
            cross_section = [
                grayscale[round(y + normal_y * offset), round(x + normal_x * offset)]
                for offset in range(-6, 7)
            ]
            strengths.append(float(np.mean(np.asarray(cross_section) < 120)))
        smoothed = np.convolve(strengths, np.ones(5) / 5, mode="same")
        low_strength = smoothed < 0.13
        runs = []
        run_start = None
        for index, is_gap in enumerate(low_strength.tolist() + [False]):
            if is_gap and run_start is None:
                run_start = index
            elif not is_gap and run_start is not None:
                if index - run_start >= 8:
                    runs.append((run_start, index - 1, float(smoothed[run_start:index].mean())))
                run_start = None

        for run_start, run_end, mean_strength in runs:
            gap_pixels = run_end - run_start + 1
            midpoint = (run_start + run_end) / 2
            side_distance = 11
            center_x = start[0] + unit_x * midpoint
            center_y = start[1] + unit_y * midpoint
            side_a = room_at((center_x + normal_x * side_distance, center_y + normal_y * side_distance))
            side_b = room_at((center_x - normal_x * side_distance, center_y - normal_y * side_distance))
            if side_a == side_b:
                continue

            start_point = (start[0] + unit_x * run_start, start[1] + unit_y * run_start)
            end_point = (start[0] + unit_x * run_end, start[1] + unit_y * run_end)
            if gap_pixels * scale > profile.door_width_max_mm and (side_a is None) != (side_b is None):
                gap_score = max(0.0, min(1.0, 1 - mean_strength / 0.2))
                detected.append(
                    {
                        "kind": "window",
                        "wallEdgeId": identifier,
                        "startPixel": run_start,
                        "endPixel": run_end,
                        "confidence": round(0.55 + 0.35 * gap_score, 3),
                        "method": "wall_gap_long_run+room_outside_sampling",
                        "pixelStart": {"x": round(start_point[0], 3), "y": round(start_point[1], 3)},
                        "pixelEnd": {"x": round(end_point[0], 3), "y": round(end_point[1], 3)},
                        "arcScore": None,
                        "sideARoomId": side_a,
                        "sideBRoomId": side_b,
                    }
                )
                continue

            if not profile.door_width_min_mm <= gap_pixels * scale <= profile.door_width_max_mm:
                continue
            start_arc = best_quarter_arc(start_point)
            end_arc = best_quarter_arc(end_point)
            arc_score, arc_radius, pivot_at_start = (
                (start_arc[0], start_arc[1], True)
                if start_arc[0] >= end_arc[0]
                else (end_arc[0], end_arc[1], False)
            )
            if arc_score < 0.5:
                continue
            width_pixels = max(
                gap_pixels,
                profile.door_width_min_mm / scale,
                min(arc_radius, profile.door_width_max_mm / scale),
            )
            if pivot_at_start:
                candidate_start = run_start
                candidate_end = min(steps, run_start + width_pixels)
            else:
                candidate_end = run_end
                candidate_start = max(0, run_end - width_pixels)
            p1 = (start[0] + unit_x * candidate_start, start[1] + unit_y * candidate_start)
            p2 = (start[0] + unit_x * candidate_end, start[1] + unit_y * candidate_end)
            gap_score = max(0.0, min(1.0, 1 - mean_strength / 0.2))
            detected.append(
                {
                    "kind": "door",
                    "wallEdgeId": identifier,
                    "startPixel": candidate_start,
                    "endPixel": candidate_end,
                    "confidence": round(0.55 * arc_score + 0.45 * gap_score, 3),
                    "method": "wall_gap+quarter_arc+room_side_sampling",
                    "pixelStart": {"x": round(p1[0], 3), "y": round(p1[1], 3)},
                    "pixelEnd": {"x": round(p2[0], 3), "y": round(p2[1], 3)},
                    "arcScore": round(arc_score, 3),
                    "sideARoomId": side_a,
                    "sideBRoomId": side_b,
                }
            )

    detected.sort(key=lambda item: (item["wallEdgeId"], item["startPixel"]))
    for index, candidate in enumerate(detected, start=1):
        candidate["id"] = f"opening_candidate_{index:02d}"
        candidate["offset"] = round(candidate.pop("startPixel") * scale, 6)
        candidate["width"] = round((candidate.pop("endPixel") * scale) - candidate["offset"], 6)
    return detected


def render_overlay(polygons, segment_map, openings, room_annotations, matrix) -> None:
    image = cv2.imread(str(SOURCE), cv2.IMREAD_COLOR)
    fill = image.copy()
    colors = [(72, 187, 255), (255, 166, 77), (105, 205, 120), (190, 110, 220), (80, 220, 220)]
    for index, polygon in enumerate(polygons):
        contour = np.array(polygon.exterior.coords[:-1], dtype=np.int32)
        cv2.fillPoly(fill, [contour], colors[index % len(colors)])
    image = cv2.addWeighted(fill, 0.18, image, 0.82, 0)

    for identifier, start, end, thickness, _ in SEGMENTS:
        color = (30, 210, 50) if thickness == 180 else (255, 180, 30)
        cv2.line(image, start, end, color, 2, cv2.LINE_AA)
        cv2.circle(image, start, 2, (20, 20, 220), -1)
        cv2.circle(image, end, 2, (20, 20, 220), -1)

    for opening in openings:
        start, end = segment_map[opening["wallEdgeId"]]
        length_mm = LineString([start, end]).length * matrix[0]
        start_t = opening["offset"] / length_mm
        end_t = (opening["offset"] + opening["width"]) / length_mm
        p1 = (
            round(start[0] + (end[0] - start[0]) * start_t),
            round(start[1] + (end[1] - start[1]) * start_t),
        )
        p2 = (
            round(start[0] + (end[0] - start[0]) * end_t),
            round(start[1] + (end[1] - start[1]) * end_t),
        )
        cv2.line(image, p1, p2, (220, 30, 220), 5, cv2.LINE_AA)

    for annotation, (_, _, room_type, anchor, _) in zip(room_annotations, ROOMS, strict=True):
        cv2.circle(image, anchor, 4, (0, 0, 0), -1)
        cv2.putText(
            image,
            room_type,
            (anchor[0] - 24, anchor[1] - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.32,
            (0, 0, 0),
            1,
            cv2.LINE_AA,
        )
    cv2.imwrite(str(DEBUG_ROOT / "fp-001-layout-overlay-v2-openings.png"), image)


def measure_wall_alignment(segment_map, openings, scale: float) -> dict:
    grayscale = cv2.imread(str(SOURCE), cv2.IMREAD_GRAYSCALE)
    openings_by_edge = {}
    for opening in openings:
        openings_by_edge.setdefault(opening["wallEdgeId"], []).append(opening)
    total_hits = 0
    total_samples = 0
    per_edge = {}
    for identifier, start, end, _, _ in SEGMENTS:
        pixel_length = math.dist(start, end)
        steps = max(2, round(pixel_length))
        hits = 0
        samples = 0
        for step in range(steps + 1):
            progress = step / steps
            distance_mm = progress * pixel_length * scale
            if any(
                opening["offset"] <= distance_mm <= opening["offset"] + opening["width"]
                for opening in openings_by_edge.get(identifier, [])
            ):
                continue
            x = round(start[0] + (end[0] - start[0]) * progress)
            y = round(start[1] + (end[1] - start[1]) * progress)
            patch = grayscale[max(0, y - 4) : y + 5, max(0, x - 4) : x + 5]
            hits += int(patch.min() < 100)
            samples += 1
        per_edge[identifier] = round(hits / samples, 4) if samples else 1.0
        total_hits += hits
        total_samples += samples
    return {
        "coverage": round(total_hits / total_samples, 4),
        "samples": total_samples,
        "perEdgeCoverage": per_edge,
    }


if __name__ == "__main__":
    main()
