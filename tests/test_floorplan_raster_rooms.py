import cv2
import numpy as np

from pano_platform.floorplan.fusion import FusedRoom
from pano_platform.floorplan.raster_rooms import raster_room_polygons, reconcile_rooms

WW = 10


def _plan():
    """Two rooms side by side; the dividing wall has a 50px (5 ww) door gap."""

    ink = np.zeros((300, 500), np.uint8)
    cv2.rectangle(ink, (50, 50), (450, 250), 1, WW)
    ink[50:195, 245:255] = 1  # interior wall; ~50px (5 ww) door gap below
    return ink, ink.copy()


def test_door_gap_is_sealed_into_two_rooms():
    ink, wall = _plan()
    polygons = raster_room_polygons(ink, wall, WW)
    assert len(polygons) == 2


def test_wide_open_plan_is_not_sealed():
    ink = np.zeros((300, 500), np.uint8)
    cv2.rectangle(ink, (50, 50), (450, 250), 1, WW)
    ink[50:100, 245:255] = 1  # 150px stub gap, far wider than a door
    assert len(raster_room_polygons(ink, ink.copy(), WW)) == 1


def test_reconcile_keeps_cv_count_and_adds_uncovered_cells():
    ink, wall = _plan()
    polygons = raster_room_polygons(ink, wall, WW)
    left = FusedRoom("room_1", "other", ((60.0, 60.0), (200.0, 60.0), (200.0, 200.0), (60.0, 200.0)),
                     1.0, 1.0, "review", ("CV_ONLY_ROOM",))
    rooms, added = reconcile_rooms((left,), polygons, (300, 500), WW, 20.0)
    assert added == 1
    assert len(rooms) == 2
    assert rooms[0].id == "room_1" and "CV_RASTER_EXTENT" in rooms[0].reason_codes
    assert rooms[0].area_px2 > 140 * 140
