import numpy as np

import pano_platform.floorplan.learned_seg as learned_seg


def test_missing_weights_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("PANO_LEARNED_SEG", str(tmp_path / "nope.pth"))
    monkeypatch.setattr(learned_seg, "_STATE", {})
    assert learned_seg.learned_barrier(np.zeros((64, 64, 3), np.uint8)) is None


def test_room_cells_split_by_boundary(monkeypatch):
    classes = np.zeros((200, 300), np.uint8)
    classes[20:180, 20:280] = 4
    boundary = np.zeros_like(classes)
    boundary[20:180, 148:152] = 1  # open-plan split, no wall
    monkeypatch.setattr(learned_seg, "segment", lambda image: (classes, boundary))
    rooms = learned_seg.learned_room_polygons(np.zeros((200, 300, 3), np.uint8), 10.0)
    assert len(rooms) == 2


def test_openings_from_door_and_window_components(monkeypatch):
    classes = np.zeros((200, 300), np.uint8)
    classes[50:58, 80:170] = 1    # wall around the door
    classes[50:58, 100:150] = 2   # horizontal door
    classes[110:180, 20:27] = 1   # wall around the window
    classes[120:170, 20:27] = 3   # vertical window
    classes[10:11, 10:11] = 2     # speck, ignored
    monkeypatch.setattr(learned_seg, "segment", lambda image: (classes, np.zeros_like(classes)))
    spans = learned_seg.learned_openings(np.zeros((200, 300, 3), np.uint8), 10.0)
    kinds = sorted(kind for kind, _, _ in spans)
    assert kinds == ["door", "window"]
    door = next(span for span in spans if span[0] == "door")
    assert door[1][1] == door[2][1] and abs(door[2][0] - door[1][0] - 50) <= 1
