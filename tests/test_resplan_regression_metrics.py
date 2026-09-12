from collections import Counter

from scripts.run_resplan_regression import opening_metrics


def opening(kind, start, end):
    return {
        "type": kind,
        "pixelStart": {"x": start[0], "y": start[1]},
        "pixelEnd": {"x": end[0], "y": end[1]},
    }


def test_opening_metrics_match_by_type_endpoints_and_sample_scope() -> None:
    truth = [opening("door", (10, 20), (10, 80)), opening("window", (100, 20), (180, 20))]
    predicted = [opening("door", (11, 79), (11, 21)), opening("door", (100, 20), (180, 20))]

    result = opening_metrics(predicted, truth, tolerance_px=3)

    assert result == {
        "truePositive": 1, "falsePositive": 1, "falseNegative": 1,
        "precision": 0.5, "recall": 0.5, "f1": 0.5,
    }


def test_rejection_reason_counters_add_without_losing_categories() -> None:
    total = Counter()
    total.update({"SHORT_WINDOW_FRAGMENT": 2, "NO_ROOM": 1})
    total.update({"SHORT_WINDOW_FRAGMENT": 3})
    assert dict(sorted(total.items())) == {"NO_ROOM": 1, "SHORT_WINDOW_FRAGMENT": 5}
