from types import SimpleNamespace

import cv2
import numpy as np

from pano_platform.floorplan.fusion import FusedRoom
from pano_platform.floorplan.footprint import (
    building_footprint, clip_rooms_to_footprint, inside_fraction,
)


def _l_shaped_plan():
    """L-shaped apartment: the top-right quadrant is an exterior notch."""
    mask = np.zeros((400, 400), np.uint8)
    t = 8
    pts = np.array([(50, 50), (200, 50), (200, 200), (350, 200), (350, 350), (50, 350)], np.int32)
    cv2.polylines(mask, [pts], True, 1, t)
    # 60px (= 600mm at 10mm/px) entrance gap in the bottom wall
    mask[346:355, 120:180] = 0
    return mask


def _graph(segments=(), gaps=()):
    return SimpleNamespace(mm_per_px=10.0, wall_width_px=8.0, segments=tuple(segments), gaps=tuple(gaps))


def test_footprint_excludes_concave_exterior_notch_and_seals_door():
    footprint = building_footprint(_l_shaped_plan(), mm_per_px=10.0, wall_width_px=8.0)
    assert footprint is not None
    assert footprint[120, 120] == 1  # inside main wing
    assert footprint[300, 300] == 1  # inside lower-right wing (door sealed)
    assert footprint[100, 300] == 0  # exterior notch
    assert footprint[10, 10] == 0


def test_synthetic_bbox_boundary_segments_do_not_close_the_notch():
    bbox_top = SimpleNamespace(id="wall_boundary_h1", start=(50, 50), end=(350, 50))
    bbox_right = SimpleNamespace(id="wall_boundary_v2", start=(350, 50), end=(350, 350))
    footprint = building_footprint(
        _l_shaped_plan(), (bbox_top, bbox_right), mm_per_px=10.0, wall_width_px=8.0
    )
    assert footprint[100, 300] == 0


def test_real_wall_segments_do_close_the_footprint():
    real = SimpleNamespace(id="wall_h9", start=(200, 50), end=(350, 50))
    real_v = SimpleNamespace(id="wall_v9", start=(350, 50), end=(350, 200))
    footprint = building_footprint(
        _l_shaped_plan(), (real, real_v), mm_per_px=10.0, wall_width_px=8.0
    )
    assert footprint[100, 300] == 1


def _room(identifier, polygon):
    return FusedRoom(identifier, "other", polygon, 0.0, 0.0, "review", ("CV_ONLY_ROOM",))


def test_clip_drops_exterior_cell_and_clips_bleeding_room():
    rooms = (
        _room("inside", ((54, 54), (196, 54), (196, 346), (54, 346))),
        _room("notch", ((204, 54), (346, 54), (346, 196), (204, 196))),
        # bleeds from the lower wing up into the notch
        _room("bleed", ((204, 54), (346, 54), (346, 346), (204, 346))),
    )
    kept, dropped, clipped = clip_rooms_to_footprint(rooms, _graph(), _l_shaped_plan())
    ids = {room.id: room for room in kept}
    assert dropped == ("notch",)
    assert clipped == ("bleed",)
    assert "inside" in ids and ids["inside"].polygon_px == rooms[0].polygon_px
    bleed = ids["bleed"]
    assert min(y for _, y in bleed.polygon_px) >= 195
    assert "CV_FOOTPRINT_CLIPPED" in bleed.reason_codes
    assert abs(bleed.area_mm2 - bleed.area_px2 * 100.0) < 1e-6


def test_clip_never_empties_room_list_and_tolerates_missing_mask():
    rooms = (_room("notch", ((204, 54), (346, 54), (346, 196), (204, 196))),)
    kept, dropped, _ = clip_rooms_to_footprint(rooms, _graph(), _l_shaped_plan())
    assert kept == rooms and dropped == ()
    assert clip_rooms_to_footprint(rooms, _graph(), None)[0] == rooms


def test_inside_fraction():
    region = np.zeros((10, 10), np.uint8); region[:, :5] = 1
    footprint = np.zeros((10, 10), np.uint8); footprint[:5, :] = 1
    assert inside_fraction(region, footprint) == 0.5
