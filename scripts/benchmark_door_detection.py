from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pano_platform.opening_detection import DoorArcProposal, detect_door_arc_proposals  # noqa: E402


def read_image(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot decode {path}")
    return image


def truth_boxes(annotated: np.ndarray) -> tuple[list[tuple[int, int, int, int]], np.ndarray]:
    hsv = cv2.cvtColor(annotated, cv2.COLOR_BGR2HSV)
    red = (((hsv[:, :, 0] < 10) | (hsv[:, :, 0] > 170)) & (hsv[:, :, 1] > 120) & (hsv[:, :, 2] > 120)).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(red, 8)
    return [tuple(map(int, item[:4])) for item in stats[1:] if item[4] >= 100], red


def match(proposals: list[DoorArcProposal], boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int]:
    unmatched = set(range(len(boxes)))
    true_positive = 0
    for proposal in proposals:
        x, y = proposal.center
        matches = []
        for index in unmatched:
            left, top, width, height = boxes[index]
            diameter = max(width, height)
            if (
                left - 2 <= x <= left + width + 2
                and top - 2 <= y <= top + height + 2
                and 0.18 * diameter <= proposal.radius_px <= 1.05 * diameter
            ):
                center_x, center_y = left + width / 2, top + height / 2
                matches.append(((x - center_x) ** 2 + (y - center_y) ** 2, index))
        if matches:
            _, index = min(matches)
            unmatched.remove(index)
            true_positive += 1
    return true_positive, len(proposals) - true_positive, len(unmatched)


def summarize(rows: list[dict]) -> dict:
    true_positive = sum(row["truePositive"] for row in rows)
    false_positive = sum(row["falsePositive"] for row in rows)
    false_negative = sum(row["falseNegative"] for row in rows)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    return {
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "truePositive": true_positive, "falsePositive": false_positive, "falseNegative": false_negative,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-dir", type=Path, required=True)
    parser.add_argument("--annotated-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    names = sorted(path.name for path in args.original_dir.glob("*.png") if (args.annotated_dir / path.name).exists())
    if not names:
        raise SystemExit("no original/annotated PNG pairs")

    rows = []
    for name in names:
        original_path, annotated_path = args.original_dir / name, args.annotated_dir / name
        original, annotated = read_image(original_path), read_image(annotated_path)
        if original.shape != annotated.shape:
            raise ValueError(f"shape mismatch for {name}")
        boxes, red = truth_boxes(annotated)
        protected = cv2.dilate(red, np.ones((5, 5), np.uint8)) > 0
        source_consistency = float(np.abs(original.astype(np.int16) - annotated.astype(np.int16))[~protected].mean())
        proposals = detect_door_arc_proposals(cv2.cvtColor(original, cv2.COLOR_BGR2GRAY))
        true_positive, false_positive, false_negative = match(proposals, boxes)
        rows.append({
            "file": name,
            "originalSha256": hashlib.sha256(original_path.read_bytes()).hexdigest(),
            "annotatedSha256": hashlib.sha256(annotated_path.read_bytes()).hexdigest(),
            "width": original.shape[1], "height": original.shape[0],
            "truthCount": len(boxes), "proposalCount": len(proposals),
            "truePositive": true_positive, "falsePositive": false_positive, "falseNegative": false_negative,
            "sourceConsistencyMae": round(source_consistency, 4),
        })
    report = {
        "schemaVersion": "0.1.0",
        "matchingProtocol": "one-to-one: proposal hinge inside red component bbox and radius within 0.18x..1.05x bbox diameter",
        "train": summarize(rows[:6]), "test": summarize(rows[6:]), "all": summarize(rows), "files": rows,
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
