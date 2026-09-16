import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from pano_platform.contracts import ArtifactManifest, ArtifactType

parser = argparse.ArgumentParser()
parser.add_argument("--scene", default="living-main", choices=("living-main", "kitchen-main"))
parser.add_argument("--variant", default="v1")
args = parser.parse_args()
SCENE = args.scene
ROOM_ID = "room_living_dining" if SCENE == "living-main" else "room_kitchen"
BASE = f"{SCENE}-gpt-image-2-{args.variant}"
PANO = ROOT / "datasets/derived/fp-001/panorama" / SCENE
SEMANTIC = PANO / "control/semantic/semantic-erp-2048x1024.png"
AI = PANO / "ai" / f"{BASE}-proxy-2048x1024.jpg"
OUTPUT = PANO / "ai" / f"{BASE}.structure-qa.json"
OVERLAY = PANO / "ai" / f"{BASE}.structure-edge-overlay.jpg"
semantic = cv2.imread(str(SEMANTIC), cv2.IMREAD_COLOR)
ai = cv2.imread(str(AI), cv2.IMREAD_COLOR)
if semantic is None or ai is None or semantic.shape != ai.shape:
    raise SystemExit("aligned semantic and AI panoramas are required")

palette_bgr = np.asarray([
    (0, 0, 0), (70, 57, 230), (22, 115, 249), (235, 99, 37), (94, 197, 34), (8, 179, 234),
], np.int16)
pixels = semantic.astype(np.int16)
labels = ((pixels[..., None, :] - palette_bgr[None, None, :, :]) ** 2).sum(axis=-1).argmin(axis=-1).astype(np.uint8)


def boundaries(values: np.ndarray) -> np.ndarray:
    edge = values != np.roll(values, 1, axis=1)
    edge |= values != np.roll(values, -1, axis=1)
    edge[1:] |= values[1:] != values[:-1]
    edge[:-1] |= values[:-1] != values[1:]
    return edge


def wrapped_distance(edge: np.ndarray) -> np.ndarray:
    tiled = np.concatenate((edge, edge, edge), axis=1)
    distance = cv2.distanceTransform((~tiled).astype(np.uint8), cv2.DIST_L2, 3)
    width = edge.shape[1]
    return distance[:, width:2 * width]


reference_edge = boundaries(labels)
gray = cv2.cvtColor(ai, cv2.COLOR_BGR2GRAY)
ai_edge = cv2.Canny(cv2.GaussianBlur(gray, (0, 0), 1.1), 55, 140) > 0
distance = wrapped_distance(ai_edge)
tolerance = 6


def edge_metrics(mask: np.ndarray):
    values = distance[mask]
    return {
        "pixelCount": int(mask.sum()),
        "recallWithin6px": round(float((values <= tolerance).mean()), 4),
        "meanChamferPx": round(float(values.mean()), 4),
        "p95ChamferPx": round(float(np.percentile(values, 95)), 4),
    }


door_window_mask = (labels == 2) | (labels == 3)
door_window_edge = boundaries(door_window_mask) & np.pad(door_window_mask[:, 1:] | door_window_mask[:, :-1], ((0, 0), (0, 1)), constant_values=False)
metrics = {
    "schemaVersion": "0.1.0", "projectId": "fp001", "roomId": ROOM_ID,
    "reference": str(SEMANTIC.relative_to(ROOT)).replace("\\", "/"),
    "candidate": str(AI.relative_to(ROOT)).replace("\\", "/"),
    "method": "semantic-boundary-to-canny-chamfer-v1", "tolerancePx": tolerance,
    "allStructureEdges": edge_metrics(reference_edge),
    "doorWindowEdges": edge_metrics(door_window_edge),
    "candidateEdgeDensity": round(float(ai_edge.mean()), 4),
    "sharpnessLaplacianVariance": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 4),
    "regionIoU": None,
    "edgeF1": None,
    "status": "needsReview",
    "reasonCodes": ["EDGE_RECALL_PROXY_ONLY", "AI_SEMANTIC_SEGMENTATION_REQUIRED_FOR_IOU_AND_F1"],
}
OUTPUT.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
overlay = ai.copy()
overlay[reference_edge] = (0, 0, 255)
cv2.imwrite(str(OVERLAY), overlay, [cv2.IMWRITE_JPEG_QUALITY, 92])
manifest = ArtifactManifest(
    id=f"artifact:fp001:quality-report:{SCENE}:gpt-image-2:{args.variant}", projectId="fp001",
    artifactType=ArtifactType.QUALITY_REPORT,
    contentHash=hashlib.sha256(OUTPUT.read_bytes()).hexdigest(), byteLength=OUTPUT.stat().st_size,
    contentType="application/json", generator={"name": "semantic-edge-proxy-qa", "version": "0.1.0"},
    inputDigests=[hashlib.sha256(SEMANTIC.read_bytes()).hexdigest(), hashlib.sha256(AI.read_bytes()).hexdigest()],
    createdAt=datetime.now(timezone.utc), metadata={"status": metrics["status"], "reasonCodes": metrics["reasonCodes"]},
)
manifest_json = manifest.model_dump_json(indent=2)
(PANO / f"ai/{BASE}.qualityReport.artifact-manifest.json").write_text(manifest_json, encoding="utf-8")
if args.variant == "v1":
    (PANO / "ai/qualityReport.artifact-manifest.json").write_text(manifest_json, encoding="utf-8")
print(json.dumps({"all": metrics["allStructureEdges"], "doorsWindows": metrics["doorWindowEdges"]}, ensure_ascii=False))
