"""T07, F3: opening arbitration against the CV wall graph."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from pano_platform.floorplan.fusion_openings import arbitrate_cv_door_arcs, arbitrate_cv_windows, arbitrate_openings
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
    return OpeningDraft(type="door", evidence=["doorArc"], pixelStart=[sx, sy], pixelEnd=[ex, ey], confidence=0.9)


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
    opening = OpeningDraft(type="window", evidence=["parallelLines"], pixelStart=[0.21, 0.10], pixelEnd=[0.29, 0.10], confidence=0.9)
    result = arbitrate_openings(draft_with([opening]), graph(), 1000, 1000)
    assert not result.confirmed
    assert any("WINDOW_BETWEEN_INTERIOR_ROOMS" in item.reason_codes for item in result.candidates)


def test_exterior_window_requires_parallel_line_evidence() -> None:
    opening = OpeningDraft(type="window", pixelStart=[0.21, 0.10], pixelEnd=[0.29, 0.10], confidence=0.9)
    one_side = VlmDraft(openings=[opening], rooms=rooms()[:1], scaleCandidates=[], warnings=[])
    result = arbitrate_openings(one_side, graph(), 1000, 1000)
    assert not result.confirmed
    assert any("NO_WINDOW_PARALLEL_LINE_EVIDENCE" in item.reason_codes for item in result.candidates)


def test_cv_parallel_lines_can_confirm_exterior_window() -> None:
    opening = OpeningDraft(type="window", pixelStart=[0.21, 0.10], pixelEnd=[0.29, 0.10], confidence=0.9)
    one_side = VlmDraft(openings=[opening], rooms=rooms()[:1], scaleCandidates=[], warnings=[])
    annotation = np.zeros((1000, 1000), np.uint8)
    annotation[92, 200:301] = 1
    annotation[108, 200:301] = 1
    result = arbitrate_openings(one_side, graph(), 1000, 1000, annotation_mask=annotation)
    assert len(result.confirmed) == 1
    assert result.confirmed[0].type == "window"


def test_cv_door_arc_can_confirm_physical_gap_with_room_topology() -> None:
    proposal = SimpleNamespace(center=(250.0, 100.0), radius_px=50.0, arc_score=1.0)
    room_polygons = [
        SimpleNamespace(id="top", polygon_px=((50, 50), (450, 50), (450, 95), (50, 95))),
        SimpleNamespace(id="bottom", polygon_px=((50, 105), (450, 105), (450, 150), (50, 150))),
    ]

    result = arbitrate_cv_door_arcs([proposal], graph(), room_polygons, 1000, 1000)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].wall_edge_id == "h1g"
    assert result.confirmed[0].width_mm == 1000
    assert result.confirmed[0].side_room_ids == (("bottom",), ("top",))


def test_cv_door_arc_can_recover_leaf_that_hides_wall_gap() -> None:
    solid_graph = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("h1", "horizontal", (50.0, 100.0), (450.0, 100.0), 13.0, False),),
        gaps=(), mm_per_px=10.0, wall_width_px=13.0,
    )
    proposal = SimpleNamespace(center=(250.0, 100.0), radius_px=70.0, arc_score=1.0)
    room_polygons = [
        SimpleNamespace(id="top", polygon_px=((50, 50), (450, 50), (450, 95), (50, 95))),
        SimpleNamespace(id="bottom", polygon_px=((50, 105), (450, 105), (450, 150), (50, 150))),
    ]

    result = arbitrate_cv_door_arcs([proposal], solid_graph, room_polygons, 1000, 1000)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].wall_edge_id == "h1"
    assert 500 <= result.confirmed[0].width_mm <= 1500


def test_door_arc_quadrant_places_solid_wall_opening_on_hinge_side() -> None:
    solid_graph = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("h1", "horizontal", (50.0, 100.0), (450.0, 100.0), 13.0, False),),
        gaps=(), mm_per_px=10.0, wall_width_px=13.0,
    )
    proposal = SimpleNamespace(
        center=(250.0, 100.0), radius_px=70.0, arc_score=1.0,
        base_angle=0, use_one_sided_interval=True,
    )
    room_polygons = [
        SimpleNamespace(id="top", polygon_px=((50, 50), (450, 50), (450, 95), (50, 95))),
        SimpleNamespace(id="bottom", polygon_px=((50, 105), (450, 105), (450, 150), (50, 150))),
    ]

    result = arbitrate_cv_door_arcs([proposal], solid_graph, room_polygons, 1000, 1000)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].start_px == (250.0, 100.0)
    assert result.confirmed[0].end_px == (320.0, 100.0)
    assert result.confirmed[0].width_mm == 700.0


def test_door_arc_prefers_nearby_wall_with_space_in_swing_direction() -> None:
    graph_with_short_edge = WallGraph(
        horizontal_axes=(100.0, 110.0), vertical_axes=(50.0, 450.0),
        segments=(
            WallSegment("short", "horizontal", (50.0, 100.0), (250.0, 100.0), 13.0, False),
            WallSegment("full", "horizontal", (50.0, 110.0), (450.0, 110.0), 13.0, False),
        ),
        gaps=(), mm_per_px=10.0, wall_width_px=13.0,
    )
    proposal = SimpleNamespace(
        center=(250.0, 100.0), radius_px=70.0, arc_score=1.0,
        base_angle=0, use_one_sided_interval=True,
    )
    room_polygons = [
        SimpleNamespace(id="top", polygon_px=((50, 40), (450, 40), (450, 105), (50, 105))),
        SimpleNamespace(id="bottom", polygon_px=((50, 115), (450, 115), (450, 170), (50, 170))),
    ]

    result = arbitrate_cv_door_arcs([proposal], graph_with_short_edge, room_polygons, 1000, 1000)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].wall_edge_id == "full"
    assert result.confirmed[0].start_px == (250.0, 110.0)
    assert result.confirmed[0].end_px == (320.0, 110.0)


def test_strong_arc_tolerates_bootstrap_scale_underestimate() -> None:
    solid_graph = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("h1", "horizontal", (50.0, 100.0), (450.0, 100.0), 13.0, False),),
        gaps=(), mm_per_px=10.0, wall_width_px=13.0,
    )
    proposal = SimpleNamespace(center=(250.0, 100.0), radius_px=20.0, arc_score=1.0)
    room_polygons = [
        SimpleNamespace(id="top", polygon_px=((50, 50), (450, 50), (450, 95), (50, 95))),
        SimpleNamespace(id="bottom", polygon_px=((50, 105), (450, 105), (450, 150), (50, 150))),
    ]

    result = arbitrate_cv_door_arcs([proposal], solid_graph, room_polygons, 1000, 1000)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].width_mm == 400
    assert "CV_ARC_BOOTSTRAP_SCALE_TOLERANCE" in result.confirmed[0].reason_codes


def test_physical_gap_uses_same_bootstrap_scale_tolerance() -> None:
    narrow_gap = graph(mm_per_px=4.0)
    proposal = SimpleNamespace(center=(250.0, 100.0), radius_px=50.0, arc_score=1.0)
    room_polygons = [
        SimpleNamespace(id="top", polygon_px=((50, 50), (450, 50), (450, 95), (50, 95))),
        SimpleNamespace(id="bottom", polygon_px=((50, 105), (450, 105), (450, 150), (50, 150))),
    ]

    result = arbitrate_cv_door_arcs([proposal], narrow_gap, room_polygons, 1000, 1000)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].width_mm == 400
    assert "CV_ARC_BOOTSTRAP_SCALE_TOLERANCE" in result.confirmed[0].reason_codes


def test_high_score_arc_on_physical_gap_can_continue_when_cv_rooms_are_nested() -> None:
    proposal = SimpleNamespace(center=(250.0, 100.0), radius_px=50.0, arc_score=1.0)
    nested_room = SimpleNamespace(
        id="outer", polygon_px=((40, 40), (460, 40), (460, 160), (40, 160))
    )

    result = arbitrate_cv_door_arcs([proposal], graph(), [nested_room], 1000, 1000)

    assert len(result.confirmed) == 1
    assert "CV_ARC_SHARED_ROOM_PHYSICAL_GAP" in result.confirmed[0].reason_codes


def test_two_arcs_subdivide_one_oversized_physical_gap() -> None:
    oversized = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("gap", "horizontal", (150.0, 100.0), (350.0, 100.0), 13.0, True),),
        gaps=(GapCandidate("gap", "horizontal", (150.0, 100.0), (350.0, 100.0), 200.0),),
        mm_per_px=10.0, wall_width_px=13.0,
    )
    proposals = [
        SimpleNamespace(center=(220.0, 100.0), radius_px=60.0, arc_score=1.0),
        SimpleNamespace(center=(270.0, 100.0), radius_px=60.0, arc_score=1.0),
    ]
    nested_room = SimpleNamespace(
        id="outer", polygon_px=((40, 40), (460, 40), (460, 160), (40, 160))
    )

    result = arbitrate_cv_door_arcs(proposals, oversized, [nested_room], 1000, 1000)

    assert len(result.confirmed) == 2
    assert all("CV_ARC_SUBDIVIDED_PHYSICAL_GAP" in item.reason_codes for item in result.confirmed)
    first, second = result.confirmed
    assert first.end_px[0] < second.start_px[0]


def test_cv_parallel_lines_confirm_exterior_window_without_vlm() -> None:
    annotation = np.zeros((1000, 1000), np.uint8)
    annotation[92, 200:301] = 1
    annotation[108, 200:301] = 1
    room_above = SimpleNamespace(
        id="top", polygon_px=((50, 20), (450, 20), (450, 95), (50, 95))
    )

    result = arbitrate_cv_windows(graph(), [room_above], annotation)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].type == "window"
    assert result.confirmed[0].wall_edge_id == "h1g"


def test_parallel_panes_can_cut_an_existing_exterior_wall() -> None:
    solid_graph = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("solid", "horizontal", (50.0, 100.0), (450.0, 100.0), 13.0, False),),
        gaps=(), mm_per_px=10.0, wall_width_px=13.0,
    )
    ink = np.zeros((200, 500), np.uint8)
    ink[92, 200:301] = 1
    ink[108, 200:301] = 1
    room_above = SimpleNamespace(
        id="top", polygon_px=((50, 20), (450, 20), (450, 70), (50, 70))
    )

    result = arbitrate_cv_windows(solid_graph, [room_above], ink)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].wall_edge_id == "solid"
    assert "CV_WINDOW_WALL_LINES_AUTOMATIC" in result.confirmed[0].reason_codes


def test_full_axis_panes_are_not_truncated_by_short_carrier_edge() -> None:
    short_graph = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("short", "horizontal", (250.0, 100.0), (300.0, 100.0), 13.0, False),),
        gaps=(), mm_per_px=10.0, wall_width_px=13.0,
    )
    ink = np.zeros((200, 500), np.uint8)
    ink[92, 200:301] = 1
    ink[108, 200:301] = 1
    room_above = SimpleNamespace(
        id="top", polygon_px=((150, 20), (350, 20), (350, 70), (150, 70))
    )

    result = arbitrate_cv_windows(short_graph, [room_above], ink)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].wall_edge_id == "short"
    assert result.confirmed[0].start_px == (200, 100.0)
    assert result.confirmed[0].end_px == (300, 100.0)


def test_parallel_panes_overlapping_a_door_arc_are_not_a_window() -> None:
    solid_graph = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("solid", "horizontal", (50.0, 100.0), (450.0, 100.0), 13.0, False),),
        gaps=(), mm_per_px=10.0, wall_width_px=13.0,
    )
    ink = np.zeros((200, 500), np.uint8)
    ink[92, 200:301] = 1
    ink[108, 200:301] = 1
    room_above = SimpleNamespace(
        id="top", polygon_px=((50, 20), (450, 20), (450, 95), (50, 95))
    )
    door_arc = SimpleNamespace(center=(250.0, 100.0), radius_px=50.0)

    result = arbitrate_cv_windows(solid_graph, [room_above], ink, [door_arc])

    assert not result.confirmed
    assert "CV_WINDOW_WALL_LINES_DOOR_ARC_OVERLAP" in result.rejected[0].reason_codes


def test_short_parallel_panes_are_recorded_as_rejected_window_fragment() -> None:
    solid_graph = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("solid", "horizontal", (50.0, 100.0), (450.0, 100.0), 13.0, False),),
        gaps=(), mm_per_px=10.0, wall_width_px=13.0,
    )
    ink = np.zeros((200, 500), np.uint8)
    ink[92, 200:226] = 1
    ink[108, 200:226] = 1
    room_above = SimpleNamespace(
        id="top", polygon_px=((50, 20), (450, 20), (450, 70), (50, 70))
    )

    result = arbitrate_cv_windows(solid_graph, [room_above], ink)

    assert not result.confirmed
    assert "CV_WINDOW_WALL_LINES_WIDTH_OUT_OF_PROFILE" in result.rejected[0].reason_codes


def test_exterior_double_lines_allow_bootstrap_scale_tolerance(monkeypatch) -> None:
    from pano_platform.floorplan import fusion_openings

    solid_graph = WallGraph(
        horizontal_axes=(100.0,), vertical_axes=(50.0, 450.0),
        segments=(WallSegment("solid", "horizontal", (50.0, 100.0), (450.0, 100.0), 13.0, False),),
        gaps=(), mm_per_px=10.0, wall_width_px=13.0,
    )
    ink = np.zeros((200, 500), np.uint8)
    ink[92, 200:226] = 1
    ink[108, 200:226] = 1
    room_above = SimpleNamespace(
        id="top", polygon_px=((50, 20), (450, 20), (450, 70), (50, 70))
    )
    monkeypatch.setattr(fusion_openings, "_pixel_exterior_sides", lambda *args: (True, False))

    result = arbitrate_cv_windows(solid_graph, [room_above], ink)

    assert len(result.confirmed) == 1
    assert "CV_WINDOW_BOOTSTRAP_SCALE_TOLERANCE" in result.confirmed[0].reason_codes


def test_window_is_not_duplicated_across_gap_and_wall_paths() -> None:
    g = graph()
    spanning = WallSegment("solid", "horizontal", (50.0, 100.0), (450.0, 100.0), 13.0, False)
    g = WallGraph(g.horizontal_axes, g.vertical_axes, (*g.segments, spanning), g.gaps, g.mm_per_px, 13.0)
    ink = np.zeros((200, 500), np.uint8)
    ink[92, 200:301] = 1
    ink[108, 200:301] = 1
    room_above = SimpleNamespace(
        id="top", polygon_px=((50, 20), (450, 20), (450, 95), (50, 95))
    )

    result = arbitrate_cv_windows(g, [room_above], ink)

    assert len(result.confirmed) == 1
    assert result.confirmed[0].wall_edge_id == "h1g"


def test_cv_window_between_two_rooms_stays_rejected() -> None:
    annotation = np.zeros((1000, 1000), np.uint8)
    annotation[92, 200:301] = 1
    annotation[108, 200:301] = 1
    room_above = SimpleNamespace(
        id="top", polygon_px=((50, 20), (450, 20), (450, 95), (50, 95))
    )
    room_below = SimpleNamespace(
        id="bottom", polygon_px=((50, 105), (450, 105), (450, 180), (50, 180))
    )

    result = arbitrate_cv_windows(graph(), [room_above, room_below], annotation)

    assert not result.confirmed
    assert "CV_WINDOW_BETWEEN_INTERIOR_ROOMS" in result.rejected[0].reason_codes
