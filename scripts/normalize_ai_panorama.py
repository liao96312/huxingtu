import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from generate_tuzi_panorama import KITCHEN_PROMPT, PROMPT
from pano_platform.contracts import ArtifactManifest, ArtifactType, PanoManifest, canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--scene", default="living-main", choices=("living-main", "kitchen-main"))
parser.add_argument("--variant", default="v1")
args = parser.parse_args()
SCENE = args.scene
PROMPT_TEXT = PROMPT if SCENE == "living-main" else KITCHEN_PROMPT
BASE = f"{SCENE}-gpt-image-2-{args.variant}"
PANORAMA = ROOT / "datasets/derived/fp-001/panorama" / SCENE
AI = PANORAMA / "ai"
SOURCE = PANORAMA / f"{SCENE}-erp-2048x1024.png"
RAW = AI / f"{BASE}.png"
OUTPUT = AI / f"{BASE}-normalized-2048x1024.jpg"
PUBLIC = ROOT / "apps/pano-viewer/public/demo"
image = cv2.imread(str(RAW), cv2.IMREAD_COLOR)
if image is None or image.shape[1] != image.shape[0] * 2:
    raise SystemExit("raw AI image is not a valid 2:1 panorama")
raw_seam = float(np.abs(image[:, 0].astype(np.int16) - image[:, -1].astype(np.int16)).mean())
normalized = cv2.resize(image, (2048, 1024), interpolation=cv2.INTER_LANCZOS4)
strip = 96
for distance in range(strip):
    alpha = 0.5 * (1 + np.cos(np.pi * distance / strip))
    left = normalized[:, distance].astype(np.float32)
    right = normalized[:, -1 - distance].astype(np.float32)
    average = (left + right) / 2
    normalized[:, distance] = np.clip(left * (1 - alpha) + average * alpha, 0, 255)
    normalized[:, -1 - distance] = np.clip(right * (1 - alpha) + average * alpha, 0, 255)
cv2.imwrite(str(OUTPUT), normalized, [cv2.IMWRITE_JPEG_QUALITY, 94])
output_bytes = OUTPUT.read_bytes()
seam = float(np.abs(normalized[:, 0].astype(np.int16) - normalized[:, -1].astype(np.int16)).mean())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


record_path = AI / f"{BASE}.request.json"
existing_record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
request_record = {
    **existing_record,
    "schemaVersion": "0.1.0", "provider": "tuzi", "endpoint": "/v1/images/edits",
    "model": "gpt-image-2", "quality": "2k", "requestedSize": "2048x1024",
    "sourceFile": str(SOURCE.relative_to(ROOT)).replace("\\", "/"), "sourceSha256": sha(SOURCE),
    "promptSha256": hashlib.sha256(PROMPT_TEXT.encode("utf-8")).hexdigest(),
    "rawFile": str(RAW.relative_to(ROOT)).replace("\\", "/"), "rawSha256": sha(RAW),
    "rawWidth": image.shape[1], "rawHeight": image.shape[0], "rawSeamMae": round(raw_seam, 4),
    "outputFile": str(OUTPUT.relative_to(ROOT)).replace("\\", "/"), "outputSha256": sha(OUTPUT),
    "outputWidth": 2048, "outputHeight": 1024, "outputSeamMae": round(seam, 4),
    "normalization": {"resize": "lanczos4", "seamBlendStripPx": strip, "jpegQuality": 94},
    "status": "needsStructureReview", "createdAt": datetime.now(timezone.utc).isoformat(),
}
record_path.write_text(json.dumps(request_record, ensure_ascii=False, indent=2), encoding="utf-8")
reference = json.loads((PANORAMA / "panorama-reference.index.json").read_text(encoding="utf-8"))
manifest = ArtifactManifest(
    id=f"artifact:fp001:panorama-result:{SCENE}:gpt-image-2:v1", projectId="fp001",
    artifactType=ArtifactType.PANORAMA_RESULT, contentHash=sha(OUTPUT), byteLength=len(output_bytes),
    contentType="image/jpeg", generator={"name": "tuzi-gpt-image-2+wrap-normalize", "version": "0.1.0"},
    inputDigests=[reference["sha256"]], createdAt=datetime.now(timezone.utc), metadata=request_record,
)
manifest_json = manifest.model_dump_json(indent=2)
(AI / f"{BASE}.panoramaResult.artifact-manifest.json").write_text(manifest_json, encoding="utf-8")
if args.variant == "v1":
    (AI / "panoramaResult.artifact-manifest.json").write_text(manifest_json, encoding="utf-8")
shutil.copy2(OUTPUT, PUBLIC / OUTPUT.name)
if SCENE != "living-main" or args.variant != "v1":
    print(f"normalized {SCENE}; seam {raw_seam:.4f} -> {seam:.4f}")
    raise SystemExit(0)
url = f"/demo/{OUTPUT.name}"
viewer_payload = {
    "schemaVersion": "0.1.0", "projectId": "fp001", "revision": 5,
    "defaultSceneId": "living-main-ai-v1", "projection": "equirectangular",
    "scenes": [{
        "id": "living-main-ai-v1", "roomId": "room_living_dining", "roomName": "客餐厅",
        "title": "客餐厅 AI 效果 v1", "order": 0,
        "assets": {
            "original": {"url": url, "width": 2048, "height": 1024, "contentType": "image/jpeg"},
            "proxy2k": {"url": url, "width": 2048, "height": 1024, "contentType": "image/jpeg"},
            "proxy1k": {"url": url, "width": 2048, "height": 1024, "contentType": "image/jpeg"},
            "thumbnailUrl": url,
        },
        "initialView": {"yaw": 0.0, "pitch": 0.0, "fov": 82.0},
    }],
    "share": {"title": "fp-001 客餐厅 AI 全景 v1"},
}
viewer_payload["contentHash"] = canonical_sha256(viewer_payload)
viewer = PanoManifest.model_validate(viewer_payload)
(PUBLIC / "ai-manifest.json").write_text(viewer.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
print(f"normalized panorama; seam {raw_seam:.4f} -> {seam:.4f}")
