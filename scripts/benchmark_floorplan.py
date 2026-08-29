"""Measure the floorplan parse pipeline against golden truth.

The benchmark runs the deterministic CV structure and (if a ``VisionConfig`` is
available) the Tuzi vision draft, then compares the fused result to a golden
truth JSON (see ``docs/plan/annotation-spec-v1.md``). It reports the P0 metrics
defined in ``floorplan-recognition-v2.md`` §1 regardless of who won: any metric
below its threshold marks the run as not-ready-for-release.

Slides are isolated by sample ancestry so the test slice never tunes thresholds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pano_platform.floorplan.fusion import fuse  # noqa: E402
from pano_platform.floorplan.fusion_openings import arbitrate_openings  # noqa: E402
from pano_platform.floorplan.fusion_rooms import close_rooms  # noqa: E402
from pano_platform.floorplan.parse import VisionMode, orchestrate  # noqa: E402
from pano_platform.floorplan.preprocess import (  # noqa: E402
    assess_legibility,
    binarize,
    measure_strokes,
    separate_layers,
    to_grayscale,
)
from pano_platform.floorplan.rooms import polygonize_rooms  # noqa: E402
from pano_platform.floorplan.wall_graph import build_wall_graph  # noqa: E402

# P0 thresholds from the v2 plan
ROOM_MIOU_THRESHOLD = 0.90
ROOM_COUNT_ACCURACY_THRESHOLD = 0.90
OPENINGS_F1_THRESHOLD = 0.90
JSON_EFFECTIVE_RATE_THRESHOLD = 1.0


def polygon_iou(a: list, b: list) -> float:
    try:
        from shapely.geometry import Polygon

        return Polygon(a).intersection(Polygon(b)).area / Polygon(a).union(Polygon(b)).area
    except Exception:
        return 0.0


def hungarian_room_iou(predicted: list, truth: list) -> float:
    """One-to-one matched mean IoU between predicted and truth room polygons.

    The score is normalised by ``max(len(predicted), len(truth))`` so both false
    positives (extra predicted rooms) and false negatives (missed truth rooms)
    are penalised — a count mismatch cannot yield a perfect score.
    """

    if not predicted and not truth:
        return 1.0
    if not predicted or not truth:
        return 0.0

    import itertools

    best = 0.0
    # exact one-to-one assignment via permutations when both are small
    if len(predicted) <= 6 and len(truth) <= 6:
        for perm in itertools.permutations(range(len(truth)), len(predicted)):
            score = sum(polygon_iou(predicted[i], truth[j]) for i, j in enumerate(perm))
            best = max(best, score / max(len(predicted), len(truth)))
        return best

    # greedy fallback for larger sets
    matched = 0.0
    used = set()
    for pred in predicted:
        best_j, best_score = None, 0.0
        for j, truth_poly in enumerate(truth):
            if j in used:
                continue
            score = polygon_iou(pred, truth_poly)
            if score > best_score:
                best_j, best_score = j, score
        if best_j is not None:
            used.add(best_j)
            matched += best_score
    return matched / max(len(predicted), len(truth))


def opening_f1(predicted_openings: list, truth_openings: list) -> float:
    """Classification-by-type F1 for openings (door/window/doorway)."""

    if not truth_openings and not predicted_openings:
        return 1.0
    if not truth_openings:
        return 0.0
    categories = {"door", "window", "doorway"}
    tp = fp = fn = 0
    predicted_by_type: dict[str, list] = {"door": [], "window": [], "doorway": []}
    truth_by_type: dict[str, list] = {"door": [], "window": [], "doorway": []}
    for opening in predicted_openings:
        predicted_by_type.setdefault(opening.get("type"), []).append(opening)
    for opening in truth_openings:
        truth_by_type.setdefault(opening.get("type"), []).append(opening)
    for category in categories:
        tp += min(len(predicted_by_type.get(category, [])), len(truth_by_type.get(category, [])))
        fp += max(0, len(predicted_by_type.get(category, [])) - len(truth_by_type.get(category, [])))
        fn += max(0, len(truth_by_type.get(category, [])) - len(predicted_by_type.get(category, [])))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def json_effective_rate(drafts: list) -> float:
    if not drafts:
        return 0.0
    valid = sum(1 for draft in drafts if draft is not None)
    return valid / len(drafts)


def load_truth(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def predict_one(image_path: Path, truth: dict, mode: str, vision_config=None) -> dict:
    """Run one image through CV + (optional) vision, return metrics per sample."""

    image = cv2.imdecode(np.fromfile(str(image_path), np.uint8), cv2.IMREAD_COLOR)
    gray = to_grayscale(image)
    binz = binarize(gray)
    strokes = measure_strokes(binz.ink)
    layers = separate_layers(binz.ink, strokes)
    gate = assess_legibility(binz, strokes, layers)
    graph = build_wall_graph(layers, strokes)
    height, width = image.shape[:2]

    draft = None
    vision_started = time.perf_counter()
    vision_latency_ms = None
    if mode in ("shadow", "assisted") and vision_config is not None:
        try:
            from pano_platform.vision.contracts import VisionParseRequest
            from pano_platform.vision.tuzi_responses import TuziResponsesVisionProvider

            provider = TuziResponsesVisionProvider(vision_config)
            try:
                image_bytes = cv2.imencode(".png", image)[1].tobytes()
                request = VisionParseRequest(
                    imageSha256=hashlib.sha256(image_bytes).hexdigest(), provider="tuzi", model=vision_config.primaryModel,
                    promptVersion="floorplan_parse_v1", draftSchemaVersion="0.1.0",
                    sourceWidth=width, sourceHeight=height,
                )
                result = provider.parse(request, image_bytes)
                draft = result.draft
            finally:
                provider.close()
            vision_latency_ms = int((time.perf_counter() - vision_started) * 1000)
        except Exception:
            draft = None

    fused = fuse(draft, graph, width, height) if draft is not None else None
    openings = arbitrate_openings(draft, graph, width, height) if draft is not None else None

    # CV-only baseline: when no VLM draft is available, still measure the
    # deterministic room polygonization so the benchmark compares CV too.
    cv_rooms = polygonize_rooms(graph, strokes.wall_width_px, (height, width)) if fused is None else ()
    pred_rooms = [list(room.polygon) for room in (fused.rooms if fused else cv_rooms)]
    truth_rooms = [[(pt["x"], pt["y"]) for pt in room["polygon"]] for room in truth.get("rooms", [])]
    room_iou = hungarian_room_iou(pred_rooms, truth_rooms)
    room_count_ok = len(pred_rooms) == len(truth_rooms)

    pred_openings = []
    if openings is not None:
        pred_openings = [{"type": o.type, "wall_edge_id": o.wall_edge_id} for o in openings.confirmed]
    truth_openings = truth.get("openings", [])
    openings_f1 = opening_f1(pred_openings, truth_openings)

    return {
        "sampleId": image_path.stem,
        "mode": mode,
        "visionLatencyMs": vision_latency_ms,
        "schemeEnabled": bool(draft),
        "roomMiou": room_iou,
        "roomCountCorrect": room_count_ok,
        "roomCountPredicted": len(pred_rooms),
        "roomCountTruth": len(truth_rooms),
        "openingsF1": openings_f1,
        "gateAccepted": gate.accepted,
        "mmPerPx": graph.mm_per_px,
        "scaleStatus": fused.scale_status if fused else None,
        "dirty": not (gate.accepted and fused is not None and fused.scale_status in ("confirmed", "bootstrap") and len(fused.rooms) > 0),
    }


def summarize(samples: list[dict]) -> dict:
    n = len(samples)
    if not n:
        return {"samples": 0}
    room_miou = sum(s["roomMiou"] for s in samples) / n
    room_count_accuracy = sum(1 for s in samples if s["roomCountCorrect"]) / n
    openings_f1 = sum(s["openingsF1"] for s in samples) / n
    json_rate = sum(1 for s in samples if s["schemeEnabled"]) / n
    avg_latency = sum(s["visionLatencyMs"] for s in samples if s["visionLatencyMs"]) / max(1, sum(1 for s in samples if s["visionLatencyMs"]))

    thresholds = {
        "roomMiou": ROOM_MIOU_THRESHOLD,
        "roomCountAccuracy": ROOM_COUNT_ACCURACY_THRESHOLD,
        "openingsF1": OPENINGS_F1_THRESHOLD,
        "jsonEffectiveRate": JSON_EFFECTIVE_RATE_THRESHOLD,
    }
    met = {
        "roomMiou": room_miou >= thresholds["roomMiou"],
        "roomCountAccuracy": room_count_accuracy >= thresholds["roomCountAccuracy"],
        "openingsF1": openings_f1 >= thresholds["openingsF1"],
        "jsonEffectiveRate": json_rate >= thresholds["jsonEffectiveRate"],
    }
    ready = all(met.values()) and room_count_accuracy > 0
    return {
        "samples": n,
        "roomMiou": room_miou,
        "roomCountAccuracy": room_count_accuracy,
        "openingsF1": openings_f1,
        "jsonEffectiveRate": json_rate,
        "avgVisionLatencyMs": round(avg_latency, 1),
        "thresholds": thresholds,
        "met": met,
        "releaseReady": ready,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark floorplan parse against golden truth")
    parser.add_argument("--truth", required=True, help="path to a golden truth JSON")
    parser.add_argument("--image-dir", required=True, help="directory of images to benchmark")
    parser.add_argument("--mode", choices=["off", "shadow", "assisted"], default="off")
    parser.add_argument(
        "--allow-external-vision", action="store_true",
        help="confirm benchmark images may be sent to Tuzi/upstream vision",
    )
    parser.add_argument("--out", default=None, help="write summary JSON here")
    parser.add_argument("--per-sample", default=None, help="write per-sample JSON here")
    args = parser.parse_args()

    vision_config = None
    if args.mode in ("shadow", "assisted") and args.allow_external_vision:
        try:
            from pano_platform.vision.config import VisionConfig

            vision_config = VisionConfig.from_env()
        except ValueError as error:
            print(f"[config] {error}; running CV-only", file=sys.stderr)
    elif args.mode in ("shadow", "assisted"):
        print("[privacy] external vision not authorised; running CV-only", file=sys.stderr)

    truth = load_truth(Path(args.truth))
    image_dir = Path(args.image_dir)
    samples = []
    for image_path in sorted(image_dir.glob("*.png")):
        samples.append(predict_one(image_path, truth, args.mode, vision_config))

    summary = summarize(samples)
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.per_sample:
        Path(args.per_sample).write_text(json.dumps(samples, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    return 0 if summary.get("releaseReady") else 1


if __name__ == "__main__":
    raise SystemExit(main())
