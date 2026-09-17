from __future__ import annotations

import json
import os
from dataclasses import dataclass
from math import atan2, cos, hypot, sin
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polylabel

from .contracts import CameraPlan, DerivedRoomSet, FurnitureItem, FurniturePlan, LayoutRevision, OpeningType, RoomCamera, canonical_sha256

WALL_MARGIN_MM = 100.0
FURNITURE_GAP_MM = 50.0
DOOR_CLEARANCE_MM = 900.0
CAMERA_CLEARANCE_MM = 300.0


@dataclass(frozen=True)
class Spec:
    kind: str
    length: float
    width: float
    height: float


PACKS = {
    "living": (Spec("sofa", 1800, 800, 750), Spec("coffeeTable", 900, 500, 400)),
    "livingdining": (Spec("sofa", 1800, 800, 750), Spec("coffeeTable", 900, 500, 400), Spec("diningTable", 1400, 800, 750)),
    "dining": (Spec("diningTable", 1400, 800, 750),),
    "bedroom": (Spec("bed", 2000, 1500, 450),),
    "study": (Spec("desk", 1200, 600, 750),),
    "kitchen": (Spec("kitchenCounter", 1600, 600, 900),),
    "bathroom": (Spec("vanity", 700, 500, 850), Spec("toilet", 700, 400, 750)),
}


def build_furniture_plan(layout: LayoutRevision, rooms: DerivedRoomSet, cameras: list[RoomCamera]) -> FurniturePlan:
    camera_by_room = {camera.roomId: Point(camera.position.x, camera.position.y) for camera in cameras}
    doors = _doors(layout)
    items: list[FurnitureItem] = []
    skipped: list[dict[str, str]] = []
    for room in sorted(rooms.rooms, key=lambda value: value.id):
        specs = PACKS.get(room.type.replace("_", "").lower(), ())
        polygon = Polygon([(point.x, point.y) for point in room.polygon])
        inner = polygon.buffer(-WALL_MARGIN_MM)
        zones = _door_zones(polygon, doors)
        footprints: list[Polygon] = []
        sofa = None
        for spec in specs:
            placement = _place(polygon, inner, spec, footprints, zones, camera_by_room.get(room.id), sofa)
            if placement is None:
                skipped.append({"roomId": room.id, "kind": spec.kind, "reason": "NO_FIT"})
                continue
            footprint, x, y, angle, inward = placement
            footprints.append(footprint)
            if spec.kind == "sofa":
                sofa = (x, y, angle, inward, spec.width)
            items.append(FurnitureItem(
                id=f"{room.id}_{spec.kind}_{len(footprints)}", roomId=room.id, kind=spec.kind,
                center={"x": x, "y": y, "z": spec.height / 2},
                length=spec.length, width=spec.width, height=spec.height, angleRadians=angle,
            ))
    payload = {
        "schemaVersion": "0.1.0", "projectId": layout.projectId,
        "layoutRevisionId": layout.id, "sourceLayoutHash": layout.contentHash,
        "sourceRoomHash": rooms.contentHash,
        "items": [item.model_dump(mode="json") for item in items], "skipped": skipped,
    }
    payload["contentHash"] = canonical_sha256(payload)
    return FurniturePlan.model_validate(payload)


def optimize_cameras_for_furniture(rooms: DerivedRoomSet, cameras: list[RoomCamera], plan: FurniturePlan) -> list[RoomCamera]:
    current = {camera.roomId: camera for camera in cameras}
    result = []
    for room in rooms.rooms:
        polygon = Polygon([(point.x, point.y) for point in room.polygon])
        items = [item for item in plan.items if item.roomId == room.id]
        if not items:
            result.append(current[room.id])
            continue
        footprints = [_rect(item.center.x, item.center.y, item.angleRadians, item.length, item.width) for item in items]
        safe = polygon.buffer(-400)
        min_x, min_y, max_x, max_y = polygon.bounds
        best = None
        for y in [min_y + (max_y - min_y) * index / 24 for index in range(25)]:
            for x in [min_x + (max_x - min_x) * index / 24 for index in range(25)]:
                point = Point(x, y)
                if safe.is_empty or not safe.covers(point):
                    continue
                distance = min(footprint.distance(point) for footprint in footprints)
                if distance < 600:
                    continue
                visible = sum(polygon.buffer(1).covers(LineString((point, footprint.centroid))) for footprint in footprints)
                score = visible * 10000 + distance + polygon.boundary.distance(point) * 0.2
                if best is None or score > best[0]:
                    best = (score, point)
        if best is None:
            result.append(current[room.id])
            continue
        point = best[1]
        result.append(RoomCamera(
            id=f"camera_{room.id}_main", roomId=room.id,
            position={"x": round(point.x, 3), "y": round(point.y, 3), "z": 1500},
            clearance=round(point.distance(polygon.boundary), 3), method="polylabel",
        ))
    return result


def load_furniture_inputs(project_id: str) -> tuple[LayoutRevision, DerivedRoomSet, list[RoomCamera]]:
    root = Path(os.environ.get("PANO_DATA_ROOT", Path(__file__).resolve().parents[2] / "datasets" / "derived"))
    folder = root / ("fp-001" if project_id == "fp001" else project_id)
    layout_paths = sorted(folder.glob("layout-revision.reviewed-v*.json")) or [folder / "layout-revision.preview-v1.json"]
    room_paths = sorted(folder.glob("rooms.manual-v*.json")) or [folder / "rooms.preview-v1.json"]
    layout = LayoutRevision.model_validate_json(layout_paths[-1].read_text(encoding="utf-8"))
    rooms = DerivedRoomSet.model_validate_json(room_paths[-1].read_text(encoding="utf-8"))
    camera_path = folder / "camera-plan.auto-v1.json"
    if camera_path.is_file():
        cameras = list(CameraPlan.model_validate_json(camera_path.read_text(encoding="utf-8")).cameras)
    else:
        cameras = []
        for room in rooms.rooms:
            polygon = Polygon([(point.x, point.y) for point in room.polygon])
            point = polylabel(polygon, tolerance=10)
            cameras.append(RoomCamera(id=f"camera_{room.id}_main", roomId=room.id, position={"x": point.x, "y": point.y, "z": 1500}, clearance=point.distance(polygon.boundary)))
    return layout, rooms, cameras


def _doors(layout: LayoutRevision):
    nodes = {node.id: node for node in layout.nodes}
    edges = {edge.id: edge for edge in layout.edges}
    result = []
    for opening in layout.openings:
        if opening.type is not OpeningType.DOOR or opening.wallEdgeId not in edges:
            continue
        edge = edges[opening.wallEdgeId]
        start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
        length = hypot(end.x - start.x, end.y - start.y)
        ux, uy = (end.x - start.x) / length, (end.y - start.y) / length
        middle = opening.offset + opening.width / 2
        result.append((start.x + ux * middle, start.y + uy * middle, ux, uy, opening.width))
    return result


def _door_zones(room: Polygon, doors) -> list[Polygon]:
    result = []
    for x, y, ux, uy, width in doors:
        if room.boundary.distance(Point(x, y)) > 250:
            continue
        nx, ny = -uy, ux
        for sign in (1, -1):
            if room.covers(Point(x + nx * sign * 150, y + ny * sign * 150)):
                result.append(_rect(x + nx * sign * DOOR_CLEARANCE_MM / 2, y + ny * sign * DOOR_CLEARANCE_MM / 2, atan2(uy, ux), width + 300, DOOR_CLEARANCE_MM))
    return result


def _place(room, inner, spec, placed, door_zones, camera, sofa):
    candidates = []
    if spec.kind == "coffeeTable" and sofa:
        x, y, angle, inward, sofa_width = sofa
        distance = sofa_width / 2 + 350 + spec.width / 2
        candidates.append((x + inward[0] * distance, y + inward[1] * distance, angle, inward))
    ring = list(room.exterior.coords)
    for (ax, ay), (bx, by) in zip(ring, ring[1:]):
        dx, dy = bx - ax, by - ay
        wall_length = hypot(dx, dy)
        if wall_length < spec.length + 200:
            continue
        ux, uy = dx / wall_length, dy / wall_length
        angle = atan2(dy, dx)
        for sign in (1, -1):
            inward = (-uy * sign, ux * sign)
            if not room.covers(Point((ax + bx) / 2 + inward[0] * 25, (ay + by) / 2 + inward[1] * 25)):
                continue
            steps = int((wall_length - spec.length - 200) // 100) + 1
            for step in range(steps):
                along = 100 + spec.length / 2 + step * 100
                candidates.append((ax + ux * along + inward[0] * (spec.width / 2 + 120), ay + uy * along + inward[1] * (spec.width / 2 + 120), angle, inward))
    for x, y, angle, inward in candidates:
        footprint = _rect(x, y, angle, spec.length, spec.width)
        if not inner.covers(footprint):
            continue
        if any(footprint.buffer(FURNITURE_GAP_MM).intersects(other) for other in placed):
            continue
        if any(footprint.intersects(zone) for zone in door_zones):
            continue
        if camera is not None and footprint.buffer(CAMERA_CLEARANCE_MM).intersects(camera):
            continue
        return footprint, round(x, 3), round(y, 3), round(angle, 6), inward
    return None


def _rect(x, y, angle, length, width):
    ux, uy, nx, ny = cos(angle), sin(angle), -sin(angle), cos(angle)
    hl, hw = length / 2, width / 2
    return Polygon([(x - ux * hl - nx * hw, y - uy * hl - ny * hw), (x + ux * hl - nx * hw, y + uy * hl - ny * hw), (x + ux * hl + nx * hw, y + uy * hl + ny * hw), (x - ux * hl + nx * hw, y - uy * hl + ny * hw)])
