import math

from shapely.geometry import Point, Polygon

from scripts.build_fp101_truth_project import build_payloads


def test_fp101_truth_project_has_nine_renderable_rooms() -> None:
    layout, rooms, cameras = build_payloads()
    assert len(rooms.rooms) == len(cameras.cameras) == 9
    assert len(layout.openings) == 17
    assert {room.type for room in rooms.rooms} >= {"livingDining", "bedroom", "kitchen", "bathroom"}

    nodes = {node.id: node for node in layout.nodes}
    edges = {edge.id: edge for edge in layout.edges}
    wall_pairs = [
        tuple(sorted(((nodes[edge.startNodeId].x, nodes[edge.startNodeId].y), (nodes[edge.endNodeId].x, nodes[edge.endNodeId].y))))
        for edge in layout.edges
    ]
    assert len(wall_pairs) == len(set(wall_pairs))

    door_centers = []
    for opening in layout.openings:
        if opening.type.value != "door":
            continue
        edge = edges[opening.wallEdgeId]
        start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
        length = math.hypot(end.x - start.x, end.y - start.y)
        ratio = (opening.offset + opening.width / 2) / length
        door_centers.append(Point(start.x + (end.x - start.x) * ratio, start.y + (end.y - start.y) * ratio))

    camera_by_room = {camera.roomId: camera for camera in cameras.cameras}
    for room in rooms.rooms:
        if room.type != "bedroom":
            continue
        polygon = Polygon([(point.x, point.y) for point in room.polygon])
        assert any(polygon.boundary.distance(center) <= 250 for center in door_centers)
        assert camera_by_room[room.id].clearance >= 650
