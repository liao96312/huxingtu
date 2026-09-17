from scripts.build_fp101_truth_project import build_payloads


def test_fp101_truth_project_has_nine_renderable_rooms() -> None:
    layout, rooms, cameras = build_payloads()
    assert len(rooms.rooms) == len(cameras.cameras) == 9
    assert len(layout.openings) == 17
    assert {room.type for room in rooms.rooms} >= {"livingDining", "bedroom", "kitchen", "bathroom"}
