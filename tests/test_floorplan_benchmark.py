"""T09: benchmark metric helpers (unit-tested offline, no network/VLM)."""

from __future__ import annotations

from scripts.benchmark_floorplan import (
    hungarian_room_iou,
    opening_f1,
    polygon_iou,
)


def _polygon(l, t, r, b):
    return [(l, t), (r, t), (r, b), (l, b)]


def test_polygon_iou_perfect() -> None:
    a = _polygon(0, 0, 10, 10)
    assert polygon_iou(a, a) == 1.0


def test_polygon_iou_disjoint() -> None:
    a = _polygon(0, 0, 10, 10)
    b = _polygon(50, 50, 60, 60)
    assert polygon_iou(a, b) == 0.0


def test_polygon_iou_partial() -> None:
    a = _polygon(0, 0, 10, 10)
    b = _polygon(5, 0, 15, 10)
    assert 0.3 < polygon_iou(a, b) < 0.4


def test_hungarian_room_iou_matches_when_perfect() -> None:
    pred = [_polygon(0, 0, 10, 10), _polygon(20, 0, 30, 10)]
    truth = pred
    assert hungarian_room_iou(pred, truth) == 1.0


def test_hungarian_room_iou_handles_count_mismatch() -> None:
    pred = [_polygon(0, 0, 10, 10)]
    truth = [_polygon(0, 0, 10, 10), _polygon(20, 0, 30, 10)]
    score = hungarian_room_iou(pred, truth)
    assert score < 1.0  # unmatched truth room penalises the mean


def test_opening_f1_by_type() -> None:
    pred = [
        {"type": "door", "startPx": [0, 0], "endPx": [10, 0]},
        {"type": "door", "startPx": [30, 0], "endPx": [40, 0]},
        {"type": "window", "startPx": [60, 0], "endPx": [90, 0]},
    ]
    truth = [
        {"type": "door", "pixelStart": {"x": 0, "y": 0}, "pixelEnd": {"x": 10, "y": 0}},
        {"type": "door", "pixelStart": {"x": 30, "y": 0}, "pixelEnd": {"x": 40, "y": 0}},
        {"type": "window", "pixelStart": {"x": 60, "y": 0}, "pixelEnd": {"x": 90, "y": 0}},
    ]
    assert opening_f1(pred, truth) == 1.0


def test_opening_f1_penalises_missing() -> None:
    pred = [{"type": "door", "startPx": [0, 0], "endPx": [10, 0]}]
    truth = [
        {"type": "door", "pixelStart": [0, 0], "pixelEnd": [10, 0]},
        {"type": "door", "pixelStart": [30, 0], "pixelEnd": [40, 0]},
        {"type": "window", "pixelStart": [60, 0], "pixelEnd": [90, 0]},
    ]
    assert 0.0 < opening_f1(pred, truth) < 1.0


def test_opening_f1_rejects_same_type_at_wrong_location() -> None:
    pred = [{"type": "door", "startPx": [200, 200], "endPx": [210, 200]}]
    truth = [{"type": "door", "pixelStart": [0, 0], "pixelEnd": [10, 0]}]
    assert opening_f1(pred, truth) == 0.0


def test_benchmark_end_to_end_on_golden() -> None:
    from pathlib import Path

    from scripts.benchmark_floorplan import predict_one, summarize

    root = Path(__file__).parents[1]
    image_dir = root / "tests/fixtures/floorplan-golden"
    truth = __import__("json").loads((image_dir / "fp-golden.truth.json").read_text(encoding="utf-8"))

    sample = predict_one(image_dir / "fp-golden.png", truth, "off")
    summary = summarize([sample])

    assert summary["samples"] == 1
    assert 0.0 <= sample["roomMiou"] <= 1.0
    # room count is compared; the CV pipeline does not yet close all rooms, so
    # the benchmark must be conservative and NOT report releaseReady.
    assert summary["releaseReady"] is False
    assert "met" in summary
    assert summary["thresholds"]["roomMiou"] == 0.90
