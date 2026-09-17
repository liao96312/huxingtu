from math import cos, sin

from shapely.geometry import Point, Polygon

from pano_platform.furniture import CAMERA_CLEARANCE_MM, FURNITURE_GAP_MM, WALL_MARGIN_MM, _door_zones, _doors, build_furniture_plan, load_furniture_inputs


def _footprint(item):
    x, y, angle = item.center.x, item.center.y, item.angleRadians
    ux, uy, nx, ny = cos(angle), sin(angle), -sin(angle), cos(angle)
    hl, hw = item.length / 2, item.width / 2
    return Polygon([(x - ux * hl - nx * hw, y - uy * hl - ny * hw), (x + ux * hl - nx * hw, y + uy * hl - ny * hw), (x + ux * hl + nx * hw, y + uy * hl + ny * hw), (x - ux * hl + nx * hw, y - uy * hl + ny * hw)])


def test_fixed_room_furniture_is_deterministic_and_clear() -> None:
    layout, rooms, cameras = load_furniture_inputs("p0-00-three-room-v1")
    first = build_furniture_plan(layout, rooms, cameras)
    second = build_furniture_plan(layout, rooms, cameras)
    assert first == second
    assert [item.kind for item in first.items] == ["sofa", "coffeeTable", "diningTable"]

    room = rooms.rooms[0]
    polygon = Polygon([(point.x, point.y) for point in room.polygon])
    inner = polygon.buffer(-WALL_MARGIN_MM)
    footprints = [_footprint(item) for item in first.items]
    for footprint in footprints:
        assert inner.covers(footprint)
    for index, footprint in enumerate(footprints):
        assert all(not footprint.buffer(FURNITURE_GAP_MM).intersects(other) for other in footprints[index + 1:])
    for zone in _door_zones(polygon, _doors(layout)):
        assert all(not footprint.intersects(zone) for footprint in footprints)
    camera = Point(cameras[0].position.x, cameras[0].position.y)
    assert all(not footprint.buffer(CAMERA_CLEARANCE_MM).intersects(camera) for footprint in footprints)
