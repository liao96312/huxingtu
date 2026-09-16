from pathlib import Path
import json

import cv2

from pano_platform.opening_detection import (
    DoorArcProposal,
    Line2D,
    PixelWallSegment,
    choose_single_door_origin,
    cluster_collinear_window_lines,
    detect_wall_anchored_door_arcs,
    detect_door_arc_proposals,
    is_door_rect_pair,
)

ROOT = Path(__file__).resolve().parents[1]


def test_door_rect_pair_uses_existing_cad_thresholds() -> None:
    assert is_door_rect_pair(120, 900, 120, True)
    assert not is_door_rect_pair(60, 900, 120, True)
    assert not is_door_rect_pair(120, 499, 120, True)
    assert not is_door_rect_pair(120, 900, 49, True)
    assert not is_door_rect_pair(120, 900, 120, False)


def test_single_door_origin_uses_distance_dispersion() -> None:
    points = [(900, 0), (636, 636), (0, 900)]
    vertices = [(0, 0), (900, 0), (900, 900), (0, 900)]

    assert choose_single_door_origin(points, vertices) == (0, 0)


def test_window_lines_cluster_within_350mm() -> None:
    lines = [
        Line2D((0, 0), (1200, 0)),
        Line2D((0, 120), (1200, 120)),
        Line2D((0, 349), (1200, 349)),
        Line2D((0, 351), (1200, 351)),
    ]

    clusters = cluster_collinear_window_lines(lines)

    assert [len(cluster) for cluster in clusters] == [3, 1]


def test_hough_door_arcs_find_closed_leaf_missing_from_wall_gap_detector() -> None:
    image = cv2.imread(str(ROOT / "datasets/source/fp-001.png"), cv2.IMREAD_GRAYSCALE)
    walls = [
        PixelWallSegment("upper", (433, 201), (433, 258)),
        PixelWallSegment("lower", (433, 258), (433, 321)),
        PixelWallSegment("bed", (302, 258), (379, 258)),
    ]
    arcs = detect_wall_anchored_door_arcs(image, walls, 28.923076923)

    assert any(arc.wall_edge_id == "upper" and 600 < arc.radius_px * 28.923076923 < 900 for arc in arcs)
    assert {arc.wall_edge_id for arc in arcs} == {"upper", "lower", "bed"}


def test_wall_filtered_proposals_keep_known_door_without_raw_hough_flood() -> None:
    image = cv2.imread(str(ROOT / "datasets/source/fp-001.png"), cv2.IMREAD_GRAYSCALE)
    proposals = detect_door_arc_proposals(image)

    assert len(proposals) < 20
    assert any(abs(item.center[0] - 436) < 8 and abs(item.center[1] - 236) < 8 for item in proposals)


def test_compass_marker_is_not_reported_as_door_arc() -> None:
    image = cv2.imread(str(ROOT / "datasets/source/desktop-9/fp-109.png"), cv2.IMREAD_GRAYSCALE)
    proposals = detect_door_arc_proposals(image)

    assert not any(item.center[0] < image.shape[1] * 0.15 and item.center[1] > image.shape[0] * 0.82 for item in proposals)


def test_compass_inner_ring_in_lower_left_margin_is_not_reported() -> None:
    image = cv2.imread(str(ROOT / "datasets/source/desktop-9/fp-108.png"), cv2.IMREAD_GRAYSCALE)
    proposals = detect_door_arc_proposals(image)

    assert not any(item.center[0] < image.shape[1] * 0.28 and item.center[1] > image.shape[0] * 0.76 for item in proposals)


def test_nearby_same_score_arc_candidates_are_stably_deduplicated() -> None:
    from pano_platform.opening_detection import _deduplicate_proposals

    proposals = [
        DoorArcProposal((118.0, 100.0), 35.0, 1.0),
        DoorArcProposal((100.0, 100.0), 40.0, 1.0),
        DoorArcProposal((250.0, 200.0), 35.0, 1.0),
    ]

    result = _deduplicate_proposals(proposals, 1.0)
    assert [(item.center, item.radius_px) for item in result] == [
        ((100.0, 100.0), 40.0), ((250.0, 200.0), 35.0)
    ]


def test_lower_hough_threshold_recovers_feedback_missed_openings() -> None:
    image_103 = cv2.imread(str(ROOT / "datasets/source/desktop-9/fp-103.png"), cv2.IMREAD_GRAYSCALE)
    image_105 = cv2.imread(str(ROOT / "datasets/source/desktop-9/fp-105.png"), cv2.IMREAD_GRAYSCALE)
    proposals_103 = detect_door_arc_proposals(image_103)
    proposals_105 = detect_door_arc_proposals(image_105)

    assert any(abs(item.center[0] - 410) < 15 and abs(item.center[1] - 142) < 15 for item in proposals_103)
    assert any(abs(item.center[0] - 354) < 15 and abs(item.center[1] - 334) < 15 for item in proposals_105)


def test_automatic_detection_artifact_contains_no_manual_annotation() -> None:
    audit = json.loads((ROOT / "datasets/derived/fp-001/automatic-door-detection.v5.json").read_text(encoding="utf-8"))
    assert audit["detector"] == "opencv-hough-circles+quarter-arc+nearest-wall+room-topology"
    assert [item["wallEdgeId"] for item in audit["accepted"]] == ["w_kitchen_left_upper"]
    assert audit["accepted"][0]["sideRoomIds"] == ["room_bath", "room_kitchen"]
