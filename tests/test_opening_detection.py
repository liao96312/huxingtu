from pathlib import Path
import json

import cv2

from pano_platform.opening_detection import (
    Line2D,
    PixelWallSegment,
    choose_single_door_origin,
    cluster_collinear_window_lines,
    detect_wall_anchored_door_arcs,
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


def test_automatic_detection_artifact_contains_no_manual_annotation() -> None:
    audit = json.loads((ROOT / "datasets/derived/fp-001/automatic-door-detection.v5.json").read_text(encoding="utf-8"))
    assert audit["detector"] == "opencv-hough-circles+quarter-arc+nearest-wall+room-topology"
    assert [item["wallEdgeId"] for item in audit["accepted"]] == ["w_kitchen_left_upper"]
    assert audit["accepted"][0]["sideRoomIds"] == ["room_bath", "room_kitchen"]
