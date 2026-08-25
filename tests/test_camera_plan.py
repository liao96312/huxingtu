from pathlib import Path

from shapely.geometry import Point, Polygon

from pano_platform.contracts import CameraPlan, DerivedRoomSet


ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "datasets/derived/fp-001"


def test_camera_plan_places_one_camera_inside_each_room() -> None:
    plan = CameraPlan.model_validate_json((DERIVED / "camera-plan.auto-v1.json").read_text(encoding="utf-8"))
    rooms = DerivedRoomSet.model_validate_json((DERIVED / "rooms.manual-v2.json").read_text(encoding="utf-8"))
    polygons = {room.id: Polygon([(point.x, point.y) for point in room.polygon]) for room in rooms.rooms}
    assert len(plan.cameras) == len(rooms.rooms) == 5
    for camera in plan.cameras:
        assert polygons[camera.roomId].covers(Point(camera.position.x, camera.position.y))
        assert camera.position.z == 1500
        assert camera.clearance > 400
