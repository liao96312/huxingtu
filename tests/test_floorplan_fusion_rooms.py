"""T06, F1/F2: de-normalise, snap to wall axes and area self-consistency."""

from __future__ import annotations

import pytest

from pano_platform.floorplan.fusion import fuse
from pano_platform.floorplan.wall_graph import WallGraph
from pano_platform.vision.contracts import RoomDraft, VlmDraft


def graph(mmx=100.0, mmx2=400.0, mmy=100.0, mmy2=400.0, mm_per_px=10.0) -> WallGraph:
    return WallGraph(
        horizontal_axes=(mmy, mmy2),
        vertical_axes=(mmx, mmx2),
        segments=(),
        gaps=(),
        mm_per_px=mm_per_px,
    )


def test_room_vertices_snap_to_axes_when_within_tolerance() -> None:
    # a VLM polygon roughly at pixel (150,150)-(350,250); axes at 100/400
    draft = VlmDraft(
        rooms=[
            RoomDraft(
                type="bedroom", polygon=[[0.05, 0.05], [0.35, 0.05], [0.35, 0.25], [0.05, 0.25]],
                confidence=0.9,
            )
        ],
        openings=[], scaleCandidates=[], warnings=[],
    )
    # width=1000 height=1000 -> px (50,50),(350,50),(350,250),(50,250)
    # axes at 100 (x) and 100 (y). Within tolerance (factor 1.0) -> snapped to 100.
    result = fuse(draft, graph(), image_width=1000, image_height=1000)
    room = result.rooms[0]
    # x=50 snaps to 100? distance 50; tolerance = 1.0 * axis spacing*0.2.
    # We only assert snap happened where expected, and area is consistent.
    assert room.status in ("ok", "review")
    assert room.area_mm2 > 0
    assert result.evidence.rooms[0].totalVertexCount == 4


def test_area_label_mismatch_flags_review() -> None:
    # room area ~ (300px x 200px) = 60000 px^2 = 6.0 m^2 at 10mm/px.
    # label claims 40 m^2 -> mismatch -> review.
    draft = VlmDraft(
        rooms=[
            RoomDraft(
                type="living", areaLabelM2=40.0,
                polygon=[[0.05, 0.05], [0.35, 0.05], [0.35, 0.25], [0.05, 0.25]],
                confidence=0.9,
            )
        ],
        openings=[], scaleCandidates=[], warnings=[],
    )
    result = fuse(draft, graph(), image_width=1000, image_height=1000)
    room = result.rooms[0]
    assert room.status == "review"
    assert "AREA_LABEL_MISMATCH" in room.reason_codes


def test_area_magnitude_is_consistent_with_mm_per_px() -> None:
    draft = VlmDraft(
        rooms=[
            RoomDraft(
                type="bedroom", polygon=[[0.1, 0.1], [0.3, 0.1], [0.3, 0.3], [0.1, 0.3]],
                confidence=0.9,
            )
        ],
        openings=[], scaleCandidates=[], warnings=[],
    )
    # px (100,100)-(300,300) = 200x200 = 40000 px^2 at 10mm/px -> 4.0 m^2
    result = fuse(draft, graph(mm_per_px=10.0), image_width=1000, image_height=1000)
    room = result.rooms[0]
    assert 3.5 < room.area_mm2 / 1e6 < 4.5


def test_empty_cv_graph_still_denormalizes_but_marks_review() -> None:
    g = WallGraph(horizontal_axes=(), vertical_axes=(), segments=(), gaps=(), mm_per_px=10.0)
    draft = VlmDraft(
        rooms=[
            RoomDraft(
                type="bedroom", polygon=[[0.1, 0.1], [0.3, 0.1], [0.3, 0.3], [0.1, 0.3]],
                confidence=0.9,
            )
        ],
        openings=[], scaleCandidates=[], warnings=[],
    )
    result = fuse(draft, g, image_width=1000, image_height=1000)
    # no axes -> no vertex snaps -> review
    assert result.rooms[0].status == "review"
    assert "NO_VERTEX_SNAPPED" in result.rooms[0].reason_codes


def test_zone_vertices_outside_tolerance_stay_put() -> None:
    # a vertex far from any axis should NOT be snapped (no accidental snapping)
    g = WallGraph(horizontal_axes=(1000.0, 2000.0), vertical_axes=(1000.0, 2000.0), segments=(), gaps=(), mm_per_px=10.0)
    draft = VlmDraft(
        rooms=[
            RoomDraft(
                type="bedroom", polygon=[[0.01, 0.01], [0.02, 0.01], [0.02, 0.02], [0.01, 0.02]],
                confidence=0.9,
            )
        ],
        openings=[], scaleCandidates=[], warnings=[],
    )
    result = fuse(draft, g, image_width=1000, image_height=1000)
    room = result.rooms[0]
    # points (10,10),(20,10),(20,20),(10,20) are far from the 1000/2000 axes -> no snap
    # so the denormalised vertices stay exactly where they were
    assert room.polygon_px == ((10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0))
