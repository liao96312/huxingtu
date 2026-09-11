"""Run the automatic upload chain against locally prepared ResPlan truth.

Reports count deltas plus per-sample spatial room/opening metrics. ResPlan images
are synthetic rasters generated from vector truth, not customer accuracy proof.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from pano_platform.pipeline import run_upload_pipeline  # noqa: E402
from scripts.benchmark_floorplan import hungarian_room_iou  # noqa: E402

DEFAULT_CORPUS = ROOT / "tmp" / "external" / "resplan" / "derived"
DEFAULT_ARTIFACTS = ROOT / "tmp" / "external" / "resplan" / "pipeline"


def opening_metrics(predicted: list[dict], truth: list[dict], tolerance_px: float = 20.0) -> dict:
    matches = []
    for predicted_index, left in enumerate(predicted):
        for truth_index, right in enumerate(truth):
            if left["type"] != right["type"]:
                continue
            ps, pe = left["pixelStart"], left["pixelEnd"]
            ts, te = right["pixelStart"], right["pixelEnd"]
            direct = (_distance(ps, ts) + _distance(pe, te)) / 2
            reverse = (_distance(ps, te) + _distance(pe, ts)) / 2
            distance = min(direct, reverse)
            if distance <= tolerance_px:
                matches.append((distance, predicted_index, truth_index))
    used_predicted, used_truth = set(), set()
    for _, predicted_index, truth_index in sorted(matches):
        if predicted_index not in used_predicted and truth_index not in used_truth:
            used_predicted.add(predicted_index)
            used_truth.add(truth_index)
    return _summarize(len(used_predicted), len(predicted) - len(used_predicted), len(truth) - len(used_truth))


def _distance(left: dict, right: dict) -> float:
    return ((left["x"] - right["x"]) ** 2 + (left["y"] - right["y"]) ** 2) ** 0.5


def _summarize(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "truePositive": tp, "falsePositive": fp, "falseNegative": fn,
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
    }


def _predicted_geometry(folder: Path) -> tuple[list[list[tuple[float, float]]], list[dict]]:
    layout = json.loads((folder / "layout-revision.preview-v1.json").read_text(encoding="utf-8"))
    rooms = json.loads((folder / "rooms.preview-v1.json").read_text(encoding="utf-8"))["rooms"]
    transform = layout["source"]["pixelToWorldTransform"]
    scale_x, scale_y, offset_x, offset_y = transform[0], transform[4], transform[2], transform[5]
    predicted_rooms = [
        [((point["x"] - offset_x) / scale_x, (point["y"] - offset_y) / scale_y) for point in room["polygon"]]
        for room in rooms
    ]
    nodes = {node["id"]: node for node in layout["nodes"]}
    edges = {edge["id"]: edge for edge in layout["edges"]}
    predicted_openings = []
    for opening in layout["openings"]:
        edge = edges[opening["wallEdgeId"]]
        start_node, end_node = nodes[edge["startNodeId"]], nodes[edge["endNodeId"]]
        dx, dy = end_node["x"] - start_node["x"], end_node["y"] - start_node["y"]
        length = (dx * dx + dy * dy) ** 0.5
        ux, uy = dx / length, dy / length
        start = {
            "x": (start_node["x"] + ux * opening["offset"] - offset_x) / scale_x,
            "y": (start_node["y"] + uy * opening["offset"] - offset_y) / scale_y,
        }
        end = {
            "x": start["x"] + ux * opening["width"] / scale_x,
            "y": start["y"] + uy * opening["width"] / scale_y,
        }
        predicted_openings.append({"type": opening["type"], "pixelStart": start, "pixelEnd": end})
    return predicted_rooms, predicted_openings


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the upload chain on prepared ResPlan truth")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    manifest = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
    args.artifacts.mkdir(parents=True, exist_ok=True)
    os.environ["PANO_DATA_ROOT"] = str(args.artifacts)

    samples = []
    for item in manifest["samples"][: args.limit]:
        sample_id = item["sampleId"]
        truth = json.loads((args.corpus / f"{sample_id}.truth.json").read_text(encoding="utf-8"))
        result = run_upload_pipeline(
            (args.corpus / f"{sample_id}.png").read_bytes(),
            project_id=sample_id,
            allow_external_vision=False,
            generate_ai=False,
        )
        truth_doors = sum(item["type"] == "door" for item in truth["openings"])
        truth_windows = sum(item["type"] == "window" for item in truth["openings"])
        predicted_doors = result.get("automaticCvDoorCount", 0)
        predicted_windows = result.get("automaticCvWindowCount", 0)
        predicted_openings = predicted_doors + predicted_windows
        predicted_room_polygons, predicted_opening_geometry = _predicted_geometry(args.artifacts / sample_id)
        truth_room_polygons = [[(point["x"], point["y"]) for point in room["polygon"]] for room in truth["rooms"]]
        opening_score = opening_metrics(predicted_opening_geometry, truth["openings"])
        door_score = opening_metrics(
            [item for item in predicted_opening_geometry if item["type"] == "door"],
            [item for item in truth["openings"] if item["type"] == "door"],
        )
        window_score = opening_metrics(
            [item for item in predicted_opening_geometry if item["type"] == "window"],
            [item for item in truth["openings"] if item["type"] == "window"],
        )
        samples.append({
            "sampleId": sample_id,
            "pipelineStatus": result.get("pipelineStatus"),
            "viewerReady": bool(result.get("viewerUrl")),
            "truthRooms": len(truth["rooms"]),
            "predictedRooms": result.get("roomCount", 0),
            "roomCountDelta": result.get("roomCount", 0) - len(truth["rooms"]),
            "truthOpenings": len(truth["openings"]),
            "predictedOpenings": predicted_openings,
            "openingCountDelta": predicted_openings - len(truth["openings"]),
            "truthDoors": truth_doors,
            "predictedDoors": predicted_doors,
            "doorCountDelta": predicted_doors - truth_doors,
            "truthWindows": truth_windows,
            "predictedWindows": predicted_windows,
            "windowCountDelta": predicted_windows - truth_windows,
            "roomMeanIoU": round(hungarian_room_iou(predicted_room_polygons, truth_room_polygons), 4),
            "openingMetrics": opening_score,
            "doorMetrics": door_score,
            "windowMetrics": window_score,
        })

    totals = {
        key: sum(item[key] for item in samples)
        for key in (
            "truthRooms", "predictedRooms", "truthOpenings", "predictedOpenings",
            "truthDoors", "predictedDoors", "truthWindows", "predictedWindows",
        )
    }
    metrics = {}
    for name in ("openingMetrics", "doorMetrics", "windowMetrics"):
        metrics[name] = _summarize(
            sum(item[name]["truePositive"] for item in samples),
            sum(item[name]["falsePositive"] for item in samples),
            sum(item[name]["falseNegative"] for item in samples),
        )
    metrics["roomMeanIoU"] = round(sum(item["roomMeanIoU"] for item in samples) / len(samples), 4) if samples else 0.0
    report = {
        "dataset": manifest["dataset"],
        "sourceVersion": manifest["sourceVersion"],
        "converterVersion": manifest.get("converterVersion"),
        "note": "Spatial regression on synthetic ResPlan rasters; not customer-domain release evidence.",
        "automaticChainReady": all(item["viewerReady"] for item in samples),
        "totals": totals,
        "metrics": metrics,
        "samples": samples,
    }
    output = args.out or args.corpus / "pipeline-regression.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["automaticChainReady"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
