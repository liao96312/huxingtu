from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polylabel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pano_platform.contracts import CameraPlan, DerivedRoomSet, LayoutRevision, canonical_sha256  # noqa: E402
from pano_platform.furniture import build_furniture_plan, optimize_cameras_for_furniture  # noqa: E402
from pano_platform.scene_geometry import build_scene_geometry  # noqa: E402

TRUTH = ROOT / "datasets/annotations/fp-101.truth.json"
OUTPUT = ROOT / "datasets/derived/fp-101"
ROOM_NAMES = {
    "bedroom": "卧室", "balcony": "阳台", "livingDining": "客餐厅",
    "bathroom": "卫生间", "kitchen": "厨房", "other": "其他",
}


def _hashed(payload: dict) -> dict:
    payload["contentHash"] = canonical_sha256(payload)
    return payload


def _stable(model):
    payload = model.model_dump(mode="json", exclude_none=True)
    payload.pop("contentHash")
    return type(model).model_validate(_hashed(payload))


def _scale(truth: dict) -> float:
    values = []
    for item in truth["scales"]:
        start, end = item["pixelStart"], item["pixelEnd"]
        values.append(item["lengthMm"] / math.hypot(end["x"] - start["x"], end["y"] - start["y"]))
    return sum(values) / len(values)


def build_payloads() -> tuple[LayoutRevision, DerivedRoomSet, CameraPlan]:
    truth = json.loads(TRUTH.read_text(encoding="utf-8"))
    scale = _scale(truth)
    room_points = {
        room["id"]: [(round(point["x"] * scale, 3), round(point["y"] * scale, 3)) for point in room["polygon"]]
        for room in truth["rooms"]
    }

    edge_points = {}
    for points in room_points.values():
        for start, end in zip(points, points[1:] + points[:1]):
            key = tuple(sorted((start, end)))
            edge_points.setdefault(key, key)
    node_points = sorted({point for edge in edge_points for point in edge})
    node_ids = {point: f"node_{index:03d}" for index, point in enumerate(node_points, 1)}
    edges = []
    edge_lines = []
    for index, (start, end) in enumerate(sorted(edge_points), 1):
        identifier = f"wall_{index:03d}"
        edges.append({
            "id": identifier, "startNodeId": node_ids[start], "endNodeId": node_ids[end],
            "thickness": 120, "height": 2800, "confidence": 1.0,
        })
        edge_lines.append((identifier, LineString((start, end))))

    openings = []
    for item in truth["openings"]:
        kind = "door" if item["type"] == "doorway" else item["type"]
        pixel_start, pixel_end = item["pixelStart"], item["pixelEnd"]
        start = (pixel_start["x"] * scale, pixel_start["y"] * scale)
        end = (pixel_end["x"] * scale, pixel_end["y"] * scale)
        midpoint = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        edge_id, line = min(edge_lines, key=lambda value: value[1].distance(Point(midpoint)))
        first, second = sorted((line.project(Point(start)), line.project(Point(end))))
        width = max(1.0, second - first)
        openings.append({
            "id": item["id"], "type": kind, "wallEdgeId": edge_id,
            "offset": round(first, 3), "width": round(width, 3),
            "height": 2100 if kind == "door" else 1200,
            **({} if kind == "door" else {"sill": 900}), "confidence": 1.0,
        })
        if line.distance(Point(midpoint)) > 250:
            raise ValueError(f"opening {item['id']} is too far from a room boundary")

    annotations = []
    derived = []
    for room in truth["rooms"]:
        polygon = Polygon(room_points[room["id"]])
        center = polygon.representative_point()
        annotations.append({
            "id": room["id"], "type": room["type"], "label": ROOM_NAMES[room["type"]],
            "anchor": {"x": center.x, "y": center.y}, "confidence": 1.0,
        })
        derived.append({
            "id": room["id"], "name": ROOM_NAMES[room["type"]], "type": room["type"],
            "polygon": [{"x": x, "y": y} for x, y in room_points[room["id"]]],
            "areaMm2": round(polygon.area, 3), "confidence": 1.0,
        })

    layout = _stable(LayoutRevision.model_validate(_hashed({
        "schemaVersion": "0.1.0", "id": "layout_fp101_truth_r1", "projectId": "fp-101",
        "revision": 1, "units": "mm", "coordinateFrame": "business-z-up-rh",
        "source": {
            "imageArtifactId": "dataset:desktop-9:fp-101",
            "pixelToWorldTransform": [scale, 0, 0, 0, scale, 0, 0, 0, 1], "scaleConfidence": 1.0,
        },
        "nodes": [{"id": node_ids[point], "x": point[0], "y": point[1]} for point in node_points],
        "edges": edges, "openings": openings, "roomAnnotations": annotations,
    })))
    rooms = _stable(DerivedRoomSet.model_validate(_hashed({
        "schemaVersion": "0.1.0", "projectId": "fp-101", "layoutRevisionId": layout.id,
        "sourceLayoutHash": layout.contentHash, "rooms": derived,
    })))
    cameras = []
    for room in rooms.rooms:
        polygon = Polygon([(point.x, point.y) for point in room.polygon])
        point = polylabel(polygon, tolerance=10)
        cameras.append({
            "id": f"camera_{room.id}_main", "roomId": room.id,
            "position": {"x": point.x, "y": point.y, "z": 1500},
            "clearance": point.distance(polygon.boundary), "method": "polylabel",
        })
    for _ in range(3):
        camera_plan = _stable(CameraPlan.model_validate(_hashed({
            "schemaVersion": "0.1.0", "projectId": "fp-101", "layoutRevisionId": layout.id,
            "sourceLayoutHash": layout.contentHash, "sourceRoomHash": rooms.contentHash,
            "cameras": [camera.model_dump(mode="json") if hasattr(camera, "model_dump") else camera for camera in cameras],
        })))
        furniture = build_furniture_plan(layout, rooms, list(camera_plan.cameras))
        cameras = optimize_cameras_for_furniture(rooms, list(camera_plan.cameras), furniture)
    camera_plan = _stable(CameraPlan.model_validate(_hashed({
        "schemaVersion": "0.1.0", "projectId": "fp-101", "layoutRevisionId": layout.id,
        "sourceLayoutHash": layout.contentHash, "sourceRoomHash": rooms.contentHash,
        "cameras": [camera.model_dump(mode="json") for camera in cameras],
    })))
    return layout, rooms, camera_plan


def main() -> None:
    layout, rooms, cameras = build_payloads()
    scene = build_scene_geometry(layout, rooms)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    documents = {
        "layout-revision.preview-v1.json": layout,
        "rooms.preview-v1.json": rooms,
        "camera-plan.auto-v1.json": cameras,
        "scene-geometry.preview-v1.json": scene,
    }
    for name, document in documents.items():
        (OUTPUT / name).write_text(document.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    print(json.dumps({"rooms": len(rooms.rooms), "openings": len(layout.openings), "scale": layout.source.pixelToWorldTransform[0]}))


if __name__ == "__main__":
    main()
