from dataclasses import dataclass

import numpy as np

from pano_platform.floorplan.fusion import FusedRoom
from pano_platform.floorplan.room_split import split_rooms_by_doors


@dataclass
class _Seg:
    id: str
    start: tuple
    end: tuple


@dataclass
class _Graph:
    segments: tuple
    mm_per_px: float = 20.0
    wall_width_px: float = 6.0


@dataclass
class _Door:
    type: str
    start_px: tuple
    end_px: tuple


def _room():
    poly = ((10.0, 10.0), (210.0, 10.0), (210.0, 110.0), (10.0, 110.0))
    return FusedRoom("room_1", "other", poly, 200 * 100 * 400.0, 20000.0, "review", ())


def _mask_with_partial_wall():
    mask = np.zeros((130, 230), np.uint8)
    mask[10:70, 107:113] = 1  # interior wall stub; the gap 70..110 is a door
    return mask


def test_split_along_confirmed_door():
    rooms, split = split_rooms_by_doors(
        (_room(),), _Graph(()), _mask_with_partial_wall(),
        (_Door("door", (110.0, 70.0), (110.0, 110.0)),),
    )
    assert split == ("room_1",)
    assert len(rooms) == 2
    assert {room.id for room in rooms} == {"room_1", "room_1_s1"}
    assert all("CV_DOOR_SPLIT" in room.reason_codes for room in rooms)


def test_no_split_without_door_or_for_windows():
    for openings in ((), (_Door("window", (110.0, 70.0), (110.0, 110.0)),)):
        rooms, split = split_rooms_by_doors(
            (_room(),), _Graph(()), _mask_with_partial_wall(), openings,
        )
        assert split == ()
        assert len(rooms) == 1


def test_tiny_remainder_is_not_split():
    mask = np.zeros((130, 230), np.uint8)
    mask[10:70, 197:203] = 1  # stub near the edge leaves a <1.5 m^2 sliver
    rooms, split = split_rooms_by_doors(
        (_room(),), _Graph(()), mask, (_Door("door", (200.0, 70.0), (200.0, 110.0)),),
    )
    assert split == ()
    assert len(rooms) == 1
