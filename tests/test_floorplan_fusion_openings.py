"""T07, F3: opening arbitration against the CV wall graph."""

from __future__ import annotations

from pano_platform.floorplan.fusion_openings import arbitrate_openings
from pano_platform.floorplan.wall_graph import GapCandidate, WallGraph, WallSegment
from pano_platform.vision.contracts import OpeningDraft, VlmDraft


def graph(mm_per_px=10.0) -> WallGraph:
    # a horizontal wall run at y=100 spanning x=50..450 (solid), with a gap.
    solid = WallSegment("h1", "horizontal", (50.0, 100.0), (200.0, 100.0), 13.0, False)
    gap = WallSegment("h1g", "horizontal", (200.0, 100.0), (300.0, 100.0), 13.0, True)
    gap_cand = GapCandidate("h1g", "horizontal", (200.0, 100.0), (300.0, 100.0), 100.0)
    return WallGraph(
        horizontal_axes=(100.0,),
        vertical_axes=(50.0, 450.0),
        segments=(solid, gap),
        gaps=(gap_cand,),
        mm_per_px=mm_per_px,
    )


def door_opening(sx, sy, ex, ey) -> OpeningDraft:
    return OpeningDraft(type="door", pixelStart=[sx, sy], pixelEnd=[ex, ey], confidence=0.9)


def rooms() -> list[dict]:
    return [
        {"type": "bedroom", "polygon": [[0.05, 0.02], [0.45, 0.02], [0.45, 0.10], [0.05, 0.10]], "confidence": 0.9},
        {"type": "living", "polygon": [[0.05, 0.10], [0.45, 0.10], [0.45, 0.18], [0.05, 0.18]], "confidence": 0.9},
    ]


def draft_with(openings, *, include_rooms=True) -> VlmDraft:
    return VlmDraft(openings=openings, rooms=rooms() if include_rooms else [], scaleCandidates=[], warnings=[])


def test_opening_in_cv_gap_is_confirmed() -> None:
    # VLM opening endpoints project onto the virtual segment (the gap) at y~100
    draft = draft_with([door_opening(0.21, 0.10, 0.29, 0.10)])
    result = arbitrate_openings(draft, graph(), 1000, 1000)
    assert len(result.confirmed) == 1
    assert result.confirmed[0].status == "confirmed"
    assert result.confirmed[0].wall_edge_id == "h1g"
    assert result.confirmed[0].width_mm == 1000
    assert result.confirmed[0].side_room_ids == (("vlm_room_001",), ("vlm_room_000",))


def test_virtual_gap_wins_over_overlapping_solid_projection() -> None:
    g = graph()
    spanning = WallSegment("h_all", "horizontal", (50.0, 100.0), (450.0, 100.0), 13.0, False)
    g = WallGraph(g.horizontal_axes, g.vertical_axes, (spanning, *g.segments), g.gaps, g.mm_per_px)
    draft = draft_with([door_opening(0.21, 0.10, 0.29, 0.10)])
    result = arbitrate_openings(draft, g, 1000, 1000)
    assert len(result.confirmed) == 1
    assert result.confirmed[0].wall_edge_id == "h1g"


def test_opening_far_from_wall_is_rejected() -> None:
    # endpoints nowhere near y=100 wall run -> no matching edge
    draft = draft_with([door_opening(0.5, 0.8, 0.6, 0.8)])
    result = arbitrate_openings(draft, graph(), 1000, 1000)
    assert len(result.rejected) == 1
    assert "NO_MATCHING_WALL_EDGE" in result.rejected[0].reason_codes


def test_duplicate_overlapping_openings_one_rejected() -> None:
    draft = draft_with([
            door_opening(0.21, 0.10, 0.29, 0.10),
            door_opening(0.22, 0.10, 0.28, 0.10),  # overlaps the first
        ])
    result = arbitrate_openings(draft, graph(), 1000, 1000)
    confirmed = [o for o in result.confirmed if o.status == "confirmed"]
    # only one can be confirmed; the overlap one must not be confirmed separately
    assert len(confirmed) <= 1


def test_out_of_profile_width_is_candidate() -> None:
    # a very wide opening on the gap (100px * 10mm/px = 1000mm) is within door
    # bounds, so build a graph with a huge gap to exceed 1500mm
    g = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("h1", "horizontal", (50.0, 100.0), (100.0, 100.0), 13.0, False),),
        gaps=(GapCandidate("g", "horizontal", (100.0, 100.0), (400.0, 100.0), 300.0),),
        mm_per_px=10.0,  # 300px * 10 = 3000mm > door max
    )
    draft = draft_with([door_opening(0.12, 0.10, 0.38, 0.10)])  # spans the 300px gap
    result = arbitrate_openings(draft, g, 1000, 1000)
    # width 3000mm out of profile -> not confirmed
    assert all(o.status != "confirmed" for o in result.confirmed)


def test_opening_without_adjacent_room_is_rejected() -> None:
    draft = draft_with([door_opening(0.21, 0.10, 0.29, 0.10)], include_rooms=False)
    result = arbitrate_openings(draft, graph(), 1000, 1000)
    assert not result.confirmed
    assert any("NO_ADJACENT_ROOM" in item.reason_codes for item in result.candidates)


def test_window_between_two_rooms_is_rejected() -> None:
    opening = OpeningDraft(type="window", pixelStart=[0.21, 0.10], pixelEnd=[0.29, 0.10], confidence=0.9)
    result = arbitrate_openings(draft_with([opening]), graph(), 1000, 1000)
    assert not result.confirmed
    assert any("WINDOW_BETWEEN_INTERIOR_ROOMS" in item.reason_codes for item in result.candidates)
