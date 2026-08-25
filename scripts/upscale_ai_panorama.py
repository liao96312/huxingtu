import argparse
import hashlib
import json
import math
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from pano_platform.contracts import ArtifactManifest, ArtifactType, PanoManifest, canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--scene", default="living-main", choices=("living-main", "kitchen-main"))
parser.add_argument("--variant", default="v1")
parser.add_argument("--source", type=Path)
parser.add_argument("--output-base")
args = parser.parse_args()
SCENE = args.scene
AI = ROOT / "datasets/derived/fp-001/panorama" / SCENE / "ai"
BASE = args.output_base or (args.source.stem if args.source else f"{SCENE}-gpt-image-2-{args.variant}")
SOURCE = args.source.resolve() if args.source else AI / f"{BASE}-normalized-2048x1024.jpg"
TOOL = ROOT / "tmp/tools/realesrgan/realesrgan-ncnn-vulkan.exe"
MODELS = TOOL.parent / "models"
TILE_DIR = AI / ".realesrgan-tiles"
OUTPUT = AI / f"{BASE}-6144x3072.jpg"
PROXY_4K = AI / f"{BASE}-proxy-4096x2048.jpg"
PROXY_2K = AI / f"{BASE}-proxy-2048x1024.jpg"
PROXY_1K = AI / f"{BASE}-proxy-1024x512.jpg"
image = cv2.imread(str(SOURCE), cv2.IMREAD_COLOR)
if image is None or image.shape[1] != image.shape[0] * 2:
    raise SystemExit("a valid 2:1 panorama source is required")
if image.shape[:2] != (1024, 2048):
    image = cv2.resize(image, (2048, 1024), interpolation=cv2.INTER_LANCZOS4)
pad, model_scale, scale = 64, 4, 3
padded = np.concatenate((image[:, -pad:], image, image[:, :pad]), axis=1)
tile_size, minimum_overlap = 512, 96


def tile_starts(length: int) -> list[int]:
    count = max(1, math.ceil((length - tile_size) / (tile_size - minimum_overlap)) + 1)
    return [round(index * (length - tile_size) / max(1, count - 1)) for index in range(count)]


def tile_weight(index: int, starts: list[int]) -> np.ndarray:
    weight = np.ones(tile_size * model_scale, np.float32)
    if index > 0:
        overlap = (starts[index - 1] + tile_size - starts[index]) * model_scale
        weight[:overlap] *= np.linspace(0, 1, overlap, dtype=np.float32)
    if index + 1 < len(starts):
        overlap = (starts[index] + tile_size - starts[index + 1]) * model_scale
        weight[-overlap:] *= np.linspace(1, 0, overlap, dtype=np.float32)
    return weight


shutil.rmtree(TILE_DIR, ignore_errors=True)
TILE_DIR.mkdir()
x_starts, y_starts = tile_starts(padded.shape[1]), tile_starts(padded.shape[0])
output_shape = (padded.shape[0] * model_scale, padded.shape[1] * model_scale)
accumulator = np.zeros((*output_shape, 3), np.float32)
weight_sum = np.zeros(output_shape, np.float32)
for y_index, y in enumerate(y_starts):
    for x_index, x in enumerate(x_starts):
        input_tile = TILE_DIR / f"tile-{y_index}-{x_index}.png"
        output_tile = TILE_DIR / f"tile-{y_index}-{x_index}-out.png"
        cv2.imwrite(str(input_tile), padded[y:y + tile_size, x:x + tile_size])
        subprocess.run([
            str(TOOL), "-i", str(input_tile), "-o", str(output_tile), "-n", "realesrgan-x4plus",
            "-s", str(model_scale), "-t", "0", "-m", str(MODELS),
        ], check=True, cwd=TOOL.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        restored = cv2.imread(str(output_tile), cv2.IMREAD_COLOR)
        if restored is None or restored.shape[:2] != (tile_size * model_scale, tile_size * model_scale):
            raise SystemExit(f"unexpected restored tile {output_tile.name}")
        weight = np.outer(tile_weight(y_index, y_starts), tile_weight(x_index, x_starts))
        ys, xs = y * model_scale, x * model_scale
        accumulator[ys:ys + tile_size * model_scale, xs:xs + tile_size * model_scale] += restored * weight[..., None]
        weight_sum[ys:ys + tile_size * model_scale, xs:xs + tile_size * model_scale] += weight
upscaled = np.clip(accumulator / np.maximum(weight_sum[..., None], 1e-6), 0, 255).astype(np.uint8)
cropped_native = upscaled[:, pad * model_scale:-pad * model_scale]
cropped = cv2.resize(cropped_native, (image.shape[1] * scale, image.shape[0] * scale), interpolation=cv2.INTER_LANCZOS4)
final_blend = 24
for distance in range(final_blend):
    alpha = 0.5 * (1 + np.cos(np.pi * distance / final_blend))
    left = cropped[:, distance].astype(np.float32)
    right = cropped[:, -1 - distance].astype(np.float32)
    average = (left + right) / 2
    cropped[:, distance] = np.clip(left * (1 - alpha) + average * alpha, 0, 255)
    cropped[:, -1 - distance] = np.clip(right * (1 - alpha) + average * alpha, 0, 255)
cv2.imwrite(str(OUTPUT), cropped, [cv2.IMWRITE_JPEG_QUALITY, 95])
cv2.imwrite(str(PROXY_4K), cv2.resize(cropped, (4096, 2048), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 93])
cv2.imwrite(str(PROXY_2K), cv2.resize(cropped, (2048, 1024), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 91])
cv2.imwrite(str(PROXY_1K), cv2.resize(cropped, (1024, 512), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 88])
seam = float(np.abs(cropped[:, 0].astype(np.int16) - cropped[:, -1].astype(np.int16)).mean())


def describe(path: Path):
    payload = path.read_bytes()
    data = cv2.imread(str(path))
    return {"file": path.name, "width": data.shape[1], "height": data.shape[0], "byteLength": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


record_path = AI / f"{BASE}.request.json"
if args.source:
    audit = {
        "source": describe(SOURCE),
        "upscale": {
            "tool": "Real-ESRGAN-ncnn-vulkan", "model": "realesrgan-x4plus", "modelScale": model_scale, "scale": scale,
            "wrapPaddingInputPx": pad, "finalSeamBlendPx": final_blend,
            "tileMode": "manual-overlap", "tileSize": tile_size, "minimumOverlap": minimum_overlap,
            "tileGrid": [len(x_starts), len(y_starts)], "outputSeamMae": round(seam, 4),
            "assets": [describe(OUTPUT), describe(PROXY_4K), describe(PROXY_2K), describe(PROXY_1K)],
        },
    }
    (AI / f"{BASE}.upscale.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(TILE_DIR, ignore_errors=True)
    print(f"Real-ESRGAN 3x -> {cropped.shape[1]}x{cropped.shape[0]}; seam MAE={seam:.4f}")
    raise SystemExit(0)
record = json.loads(record_path.read_text(encoding="utf-8"))
record["upscale"] = {
    "tool": "Real-ESRGAN-ncnn-vulkan", "model": "realesrgan-x4plus", "modelScale": model_scale, "scale": scale,
    "wrapPaddingInputPx": pad, "finalSeamBlendPx": final_blend,
    "tileMode": "manual-overlap", "tileSize": tile_size, "minimumOverlap": minimum_overlap,
    "tileGrid": [len(x_starts), len(y_starts)], "outputSeamMae": round(seam, 4),
    "assets": [describe(OUTPUT), describe(PROXY_4K), describe(PROXY_2K), describe(PROXY_1K)],
}
record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
reference = json.loads((AI.parent / "panorama-reference.index.json").read_text(encoding="utf-8"))
manifest = ArtifactManifest(
    id=f"artifact:fp001:panorama-result:{SCENE}:gpt-image-2:v1:6k", projectId="fp001",
    artifactType=ArtifactType.PANORAMA_RESULT, contentHash=sha(OUTPUT), byteLength=OUTPUT.stat().st_size,
    contentType="image/jpeg", generator={"name": "tuzi-gpt-image-2+real-esrgan-wrap", "version": "0.1.0"},
    inputDigests=[reference["sha256"]], createdAt=datetime.now(timezone.utc), metadata=record,
)
manifest_json = manifest.model_dump_json(indent=2)
(AI / f"{BASE}.panoramaResult.artifact-manifest.json").write_text(manifest_json, encoding="utf-8")
if args.variant == "v1":
    (AI / "panoramaResult.artifact-manifest.json").write_text(manifest_json, encoding="utf-8")
public = ROOT / "apps/pano-viewer/public/demo"
for asset in (OUTPUT, PROXY_4K, PROXY_2K, PROXY_1K):
    shutil.copy2(asset, public / asset.name)
scene_specs = [
    ("living-main", "v1", "room_living_dining", "客餐厅", "客餐厅 AI 效果 v1 · 6K"),
    ("kitchen-main", "v1", "room_kitchen", "厨房", "厨房 AI 效果 v1 · 6K"),
    ("living-main", "cream-v1", "room_living_dining", "客餐厅", "客餐厅 奶油风 v1 · 6K"),
]
viewer_scenes = []
for order, (scene_name, variant, room_id, room_name, title) in enumerate(scene_specs):
    scene_base = f"{scene_name}-gpt-image-2-{variant}"
    scene_ai = ROOT / "datasets/derived/fp-001/panorama" / scene_name / "ai"
    original = scene_ai / f"{scene_base}-6144x3072.jpg"
    proxy4k = scene_ai / f"{scene_base}-proxy-4096x2048.jpg"
    proxy2k = scene_ai / f"{scene_base}-proxy-2048x1024.jpg"
    proxy1k = scene_ai / f"{scene_base}-proxy-1024x512.jpg"
    if not all(path.exists() for path in (original, proxy4k, proxy2k, proxy1k)):
        continue
    for asset in (original, proxy4k, proxy2k, proxy1k):
        shutil.copy2(asset, public / asset.name)
    viewer_scenes.append({
        "id": f"{scene_name}-ai-{variant}", "roomId": room_id, "roomName": room_name, "title": title, "order": order,
        "assets": {
            "original": {"url": f"/demo/{original.name}", "width": 6144, "height": 3072, "contentType": "image/jpeg"},
            "proxy4k": {"url": f"/demo/{proxy4k.name}", "width": 4096, "height": 2048, "contentType": "image/jpeg"},
            "proxy2k": {"url": f"/demo/{proxy2k.name}", "width": 2048, "height": 1024, "contentType": "image/jpeg"},
            "proxy1k": {"url": f"/demo/{proxy1k.name}", "width": 1024, "height": 512, "contentType": "image/jpeg"},
            "thumbnailUrl": f"/demo/{proxy1k.name}",
        }, "initialView": {"yaw": 0.0, "pitch": 0.0, "fov": 82.0},
    })
viewer_payload = {
    "schemaVersion": "0.1.0", "projectId": "fp001", "revision": 5,
    "defaultSceneId": "living-main-ai-v1", "projection": "equirectangular",
    "scenes": viewer_scenes, "share": {"title": "fp-001 AI 全景 v1 · 6K"},
}
viewer_payload["contentHash"] = canonical_sha256(viewer_payload)
(public / "ai-manifest.json").write_text(PanoManifest.model_validate(viewer_payload).model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
shutil.rmtree(TILE_DIR, ignore_errors=True)
print(f"Real-ESRGAN 3x -> {cropped.shape[1]}x{cropped.shape[0]}; seam MAE={seam:.4f}")
