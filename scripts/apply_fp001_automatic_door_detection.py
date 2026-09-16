import json
from pathlib import Path

import cv2
from shapely.geometry import Point, Polygon

from pano_platform.contracts import DerivedRoomSet, LayoutRevision, OpeningType, Point2D, canonical_sha256
from pano_platform.opening_detection import PixelWallSegment, detect_wall_anchored_door_arcs
from pano_platform.opening_projection import add_projected_opening
from pano_platform.scene_geometry import build_scene_geometry
from build_fp001_manual_revision import SEGMENTS


ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "datasets" / "derived" / "fp-001"
SCALE = 28.923076923076923
source = LayoutRevision.model_validate_json(
    (DERIVED / "layout-revision.reviewed-v3.json").read_text(encoding="utf-8")
)
image = cv2.imread(str(ROOT / "datasets/source/fp-001.png"), cv2.IMREAD_GRAYSCALE)
walls = [PixelWallSegment(identifier, start, end) for identifier, start, end, *_ in SEGMENTS]
arcs = detect_wall_anchored_door_arcs(image, walls, SCALE)
segment_by_id = {wall.id: wall for wall in walls}
rooms_document = json.loads((DERIVED / "rooms.manual-v2.json").read_text(encoding="utf-8"))
rooms = {room["id"]: Polygon([(point["x"], point["y"]) for point in room["polygon"]]) for room in rooms_document["rooms"]}
nodes = {node.id: node for node in source.nodes}
edges = {edge.id: edge for edge in source.edges}


def interval_mm(arc):
    wall = segment_by_id[arc.wall_edge_id]
    offset = ((arc.pixel_start[0] - wall.start[0]) ** 2 + (arc.pixel_start[1] - wall.start[1]) ** 2) ** 0.5 * SCALE
    width = ((arc.pixel_end[0] - arc.pixel_start[0]) ** 2 + (arc.pixel_end[1] - arc.pixel_start[1]) ** 2) ** 0.5 * SCALE
    return offset, width


def overlaps_existing(wall_edge_id, offset, width):
    for opening in source.openings:
        if opening.wallEdgeId != wall_edge_id:
            continue
        intersection = max(0, min(offset + width, opening.offset + opening.width) - max(offset, opening.offset))
        union = max(offset + width, opening.offset + opening.width) - min(offset, opening.offset)
        separation = max(opening.offset - (offset + width), offset - (opening.offset + opening.width), 0)
        if intersection / union >= 0.3 or separation <= 150:
            return True
    return False


def side_rooms(wall_edge_id, offset, width):
    edge = edges[wall_edge_id]
    start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
    dx, dy = end.x - start.x, end.y - start.y
    length = (dx * dx + dy * dy) ** 0.5
    middle = offset + width / 2
    center = (start.x + dx / length * middle, start.y + dy / length * middle)
    normal = (-dy / length, dx / length)
    found = []
    for sign in (-1, 1):
        sample = Point(center[0] + sign * normal[0] * 250, center[1] + sign * normal[1] * 250)
        found.append(next((room_id for room_id, polygon in rooms.items() if polygon.covers(sample)), None))
    return found


accepted = []
ignored = []
for arc in arcs:
    offset, width = interval_mm(arc)
    if overlaps_existing(arc.wall_edge_id, offset, width):
        ignored.append({"wallEdgeId": arc.wall_edge_id, "reason": "overlaps-confirmed-opening"})
        continue
    sides = side_rooms(arc.wall_edge_id, offset, width)
    confidence = round(min(0.98, 0.7 + 0.2 * arc.arc_score), 3)
    if None in sides or sides[0] == sides[1] or confidence < 0.9:
        ignored.append({"wallEdgeId": arc.wall_edge_id, "reason": "topology-or-confidence-gate", "sideRooms": sides})
        continue
    accepted.append((arc, offset, width, sides, confidence))

if len(accepted) != 1:
    raise SystemExit(f"expected one new high-confidence door, got {len(accepted)}")

arc, offset, width, sides, confidence = accepted[0]
corrected = add_projected_opening(
    source,
    opening_id="opening_auto_hough_01",
    opening_type=OpeningType.DOOR,
    pixel_start=Point2D(x=float(arc.pixel_start[0]), y=float(arc.pixel_start[1])),
    pixel_end=Point2D(x=float(arc.pixel_end[0]), y=float(arc.pixel_end[1])),
    confidence=confidence,
    target_revision=5,
)
corrected_payload = corrected.model_dump(mode="json", exclude={"contentHash"}, exclude_none=True)
corrected_payload["id"] = "layout_fp001_auto_r5"
corrected_payload["contentHash"] = canonical_sha256(corrected_payload)
corrected = LayoutRevision.model_validate(corrected_payload)
(DERIVED / "layout-revision.reviewed-v5.json").write_text(
    corrected.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
)

audit = {
    "schemaVersion": "0.1.0",
    "projectId": "fp001",
    "sourceLayoutHash": source.contentHash,
    "detector": "opencv-hough-circles+quarter-arc+nearest-wall+room-topology",
    "accepted": [{
        "openingId": "opening_auto_hough_01",
        "wallEdgeId": arc.wall_edge_id,
        "pixelStart": {"x": float(arc.pixel_start[0]), "y": float(arc.pixel_start[1])},
        "pixelEnd": {"x": float(arc.pixel_end[0]), "y": float(arc.pixel_end[1])},
        "widthMm": float(width),
        "arcScore": arc.arc_score,
        "sideRoomIds": sides,
        "confidence": confidence,
    }],
    "ignored": ignored,
    "outputLayoutHash": corrected.contentHash,
}
audit["contentHash"] = canonical_sha256(audit)
(DERIVED / "automatic-door-detection.v5.json").write_text(
    json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
)

derived_rooms = DerivedRoomSet.model_validate_json((DERIVED / "rooms.manual-v2.json").read_text(encoding="utf-8"))
geometry = build_scene_geometry(corrected, derived_rooms)
(DERIVED / "scene-geometry.reviewed-v5.json").write_text(geometry.model_dump_json(indent=2), encoding="utf-8")
print(f"{corrected.id}: auto door {arc.wall_edge_id}, {width:.1f}mm, {sides}, {len(geometry.solids)} solids")
