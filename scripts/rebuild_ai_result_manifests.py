import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import cv2

from pano_platform.contracts import ArtifactManifest, ArtifactType, PanoManifest, canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "apps/pano-viewer/public/demo"


def describe(path: Path) -> dict:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    payload = path.read_bytes()
    return {
        "file": path.name, "width": image.shape[1], "height": image.shape[0],
        "byteLength": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
    }


for scene in ("living-main", "kitchen-main"):
    panorama = ROOT / "datasets/derived/fp-001/panorama" / scene
    reference = json.loads((panorama / "panorama-reference.index.json").read_text(encoding="utf-8"))
    ai = panorama / "ai"
    for record_path in ai.glob("*-gpt-image-2-*.request.json"):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assets = record.get("upscale", {}).get("assets", [])
        if not assets:
            continue
        original = ai / assets[0]["file"]
        proxy4k = ai / original.name.replace("-6144x3072.jpg", "-proxy-4096x2048.jpg")
        if not proxy4k.exists():
            image = cv2.imread(str(original), cv2.IMREAD_COLOR)
            cv2.imwrite(str(proxy4k), cv2.resize(image, (4096, 2048), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 93])
        record["upscale"]["assets"] = [describe(original), describe(proxy4k)] + [
            describe(ai / asset["file"]) for asset in assets[1:] if asset["width"] != 4096
        ]
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        for asset in record["upscale"]["assets"]:
            shutil.copy2(ai / asset["file"], PUBLIC / asset["file"])
        base = record_path.name.removesuffix(".request.json")
        manifest = ArtifactManifest(
            id=f"artifact:fp001:panorama-result:{base}:6k", projectId="fp001",
            artifactType=ArtifactType.PANORAMA_RESULT,
            contentHash=hashlib.sha256(original.read_bytes()).hexdigest(), byteLength=original.stat().st_size,
            contentType="image/jpeg", generator={"name": "tuzi-gpt-image-2+real-esrgan-wrap", "version": "0.1.0"},
            inputDigests=[reference["sha256"]], createdAt=datetime.now(timezone.utc), metadata=record,
        )
        manifest_json = manifest.model_dump_json(indent=2)
        (ai / f"{base}.panoramaResult.artifact-manifest.json").write_text(manifest_json, encoding="utf-8")
        if base.endswith("-v1") and "cream" not in base:
            (ai / "panoramaResult.artifact-manifest.json").write_text(manifest_json, encoding="utf-8")

scene_specs = [
    ("living-main", "living-main-gpt-image-2-v1", "living-main-ai-v1", "room_living_dining", "客餐厅", "客餐厅 AI 效果 v1 · 6K"),
    ("kitchen-main", "kitchen-main-gpt-image-2-v1", "kitchen-main-ai-v1", "room_kitchen", "厨房", "厨房 AI 效果 v1 · 6K"),
    ("living-main", "living-main-gpt-image-2-cream-v1", "living-main-ai-cream-v1", "room_living_dining", "客餐厅", "客餐厅 奶油风 v1 · 6K"),
    ("living-main", "living-main-imagen-clarity-v1", "living-main-ai-imagen-clarity-v1", "room_living_dining", "客餐厅", "客餐厅 Imagen 清晰度样张 · 6K"),
]
viewer_scenes = []
for order, (scene, base, scene_id, room_id, room_name, title) in enumerate(scene_specs):
    ai = ROOT / "datasets/derived/fp-001/panorama" / scene / "ai"
    names = {
        "original": f"{base}-6144x3072.jpg",
        "proxy4k": f"{base}-proxy-4096x2048.jpg",
        "proxy2k": f"{base}-proxy-2048x1024.jpg",
        "proxy1k": f"{base}-proxy-1024x512.jpg",
    }
    if not all((ai / name).exists() for name in names.values()):
        continue
    for name in names.values():
        shutil.copy2(ai / name, PUBLIC / name)
    sizes = {"original": (6144, 3072), "proxy4k": (4096, 2048), "proxy2k": (2048, 1024), "proxy1k": (1024, 512)}
    viewer_scenes.append({
        "id": scene_id, "roomId": room_id, "roomName": room_name, "title": title, "order": order,
        "assets": {
            **{key: {"url": f"/demo/{name}", "width": sizes[key][0], "height": sizes[key][1], "contentType": "image/jpeg"} for key, name in names.items()},
            "thumbnailUrl": f"/demo/{names['proxy1k']}",
        },
        "initialView": {"yaw": 0.0, "pitch": 0.0, "fov": 82.0},
    })
viewer = {
    "schemaVersion": "0.1.0", "projectId": "fp001", "revision": 5,
    "defaultSceneId": "living-main-ai-v1", "projection": "equirectangular", "scenes": viewer_scenes,
    "share": {"title": "fp-001 AI 全景 v1 · 6K"},
}
viewer["contentHash"] = canonical_sha256(viewer)
(PUBLIC / "ai-manifest.json").write_text(PanoManifest.model_validate(viewer).model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
print("AI panorama manifests rebuilt")
