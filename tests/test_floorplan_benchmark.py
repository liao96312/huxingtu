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
    pred = [{"type": "door"}, {"type": "door"}, {"type": "window"}]
    truth = [{"type": "door"}, {"type": "door"}, {"type": "window"}]
    assert opening_f1(pred, truth) == 1.0


def test_opening_f1_penalises_missing() -> None:
    pred = [{"type": "door"}]
    truth = [{"type": "door"}, {"type": "door"}, {"type": "window"}]
    assert 0.0 < opening_f1(pred, truth) < 1.0


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
