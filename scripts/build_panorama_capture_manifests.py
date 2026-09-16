import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pano_platform.contracts import ArtifactManifest, ArtifactType, CameraPlan, PanoManifest, canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "datasets/derived/fp-001"
PANORAMA = DERIVED / "panorama/living-main"
CUBEMAP = PANORAMA / "cubemap"
PUBLIC = ROOT / "apps/pano-viewer/public/demo"
plan_path = DERIVED / "camera-plan.auto-v1.json"
plan = CameraPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
cubemap_index_path = CUBEMAP / "cubemap.index.json"
cubemap_index = json.loads(cubemap_index_path.read_text(encoding="utf-8"))
pano_index = json.loads((PANORAMA / "panorama-reference.index.json").read_text(encoding="utf-8"))
created_at = datetime.fromisoformat(cubemap_index["generatedAt"].replace("Z", "+00:00"))
camera_manifest_path = PANORAMA / "cameraPlan.artifact-manifest.json"
camera_created_at = (
    ArtifactManifest.model_validate_json(camera_manifest_path.read_text(encoding="utf-8")).createdAt
    if camera_manifest_path.exists()
    else datetime.fromtimestamp(plan_path.stat().st_mtime, timezone.utc)
)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


manifests = [
    ArtifactManifest(
        id="artifact:fp001:camera-plan:auto-v1", projectId="fp001", artifactType=ArtifactType.CAMERA_PLAN,
        contentHash=file_hash(plan_path), byteLength=plan_path.stat().st_size, contentType="application/json",
        generator={"name": "shapely-polylabel-camera-plan", "version": "0.1.0"},
        inputDigests=[plan.sourceLayoutHash, plan.sourceRoomHash], createdAt=camera_created_at,
        metadata={"cameraCount": len(plan.cameras), "cameraPlanHash": plan.contentHash},
    ),
    ArtifactManifest(
        id="artifact:fp001:cubemap:living-main:1024", projectId="fp001", artifactType=ArtifactType.CUBEMAP_SET,
        contentHash=file_hash(cubemap_index_path), byteLength=cubemap_index_path.stat().st_size, contentType="application/json",
        generator={"name": "three-cubemap-capture", "version": "0.1.0"},
        inputDigests=[plan.contentHash], createdAt=created_at,
        metadata={"roomId": "room_living_dining", "faces": cubemap_index["assets"]},
    ),
    ArtifactManifest(
        id="artifact:fp001:panorama-reference:living-main:2048x1024", projectId="fp001", artifactType=ArtifactType.PANORAMA_REFERENCE,
        contentHash=pano_index["sha256"], byteLength=pano_index["byteLength"], contentType="image/png",
        generator={"name": "opencv-cubemap-to-erp", "version": "0.1.0"},
        inputDigests=[file_hash(cubemap_index_path)], createdAt=created_at,
        metadata=pano_index,
    ),
]
high_cubemap = PANORAMA / "cubemap-2048"
high_cubemap_index_path = high_cubemap / "cubemap.index.json"
high_pano_index_path = PANORAMA / "panorama-reference-4096x2048.index.json"
if high_cubemap_index_path.exists() and high_pano_index_path.exists():
    high_cubemap_index = json.loads(high_cubemap_index_path.read_text(encoding="utf-8"))
    high_pano_index = json.loads(high_pano_index_path.read_text(encoding="utf-8"))
    high_created_at = datetime.fromisoformat(high_cubemap_index["generatedAt"].replace("Z", "+00:00"))
    manifests.extend([
        ArtifactManifest(
            id="artifact:fp001:cubemap:living-main:2048", projectId="fp001", artifactType=ArtifactType.CUBEMAP_SET,
            contentHash=file_hash(high_cubemap_index_path), byteLength=high_cubemap_index_path.stat().st_size, contentType="application/json",
            generator={"name": "three-cubemap-capture", "version": "0.1.0"}, inputDigests=[plan.contentHash], createdAt=high_created_at,
            metadata={"roomId": "room_living_dining", "faces": high_cubemap_index["assets"]},
        ),
        ArtifactManifest(
            id="artifact:fp001:panorama-reference:living-main:4096x2048", projectId="fp001", artifactType=ArtifactType.PANORAMA_REFERENCE,
            contentHash=high_pano_index["sha256"], byteLength=high_pano_index["byteLength"], contentType="image/png",
            generator={"name": "opencv-cubemap-to-erp", "version": "0.1.0"}, inputDigests=[file_hash(high_cubemap_index_path)], createdAt=high_created_at,
            metadata=high_pano_index,
        ),
    ])
for manifest in manifests:
    name = manifest.artifactType.value
    if manifest.id.endswith(":2048") or manifest.id.endswith(":4096x2048"):
        name += "-high"
    (PANORAMA / f"{name}.artifact-manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

scene_specs = [
    ("living-main", "room_living_dining", "客餐厅", "客餐厅结构全景"),
    ("kitchen-main", "room_kitchen", "厨房", "厨房结构全景"),
    ("bed-center-main", "room_bed_center", "中部卧室", "中部卧室结构全景"),
    ("bed-left-main", "room_bed_left", "左侧卧室", "左侧卧室结构全景"),
    ("bath-main", "room_bath", "卫生间", "卫生间结构全景"),
]
scenes = []
for order, (scene_name, room_id, room_name, title) in enumerate(scene_specs):
    scene_folder = DERIVED / "panorama" / scene_name
    scene_index = json.loads((scene_folder / "panorama-reference.index.json").read_text(encoding="utf-8"))
    source = scene_folder / scene_index["file"]
    shutil.copy2(source, PUBLIC / source.name)
    asset_url = f"/demo/{source.name}"
    scenes.append({
        "id": f"{scene_name}-structural", "roomId": room_id, "roomName": room_name,
        "title": title, "order": order,
        "assets": {
            "original": {"url": asset_url, "width": 2048, "height": 1024, "contentType": "image/png"},
            "proxy2k": {"url": asset_url, "width": 2048, "height": 1024, "contentType": "image/png"},
            "proxy1k": {"url": asset_url, "width": 2048, "height": 1024, "contentType": "image/png"},
            "thumbnailUrl": asset_url,
        }, "initialView": {"yaw": 0.0, "pitch": 0.0, "fov": 82.0},
    })
pano_payload = {
    "schemaVersion": "0.1.0", "projectId": "fp001", "revision": 5,
    "defaultSceneId": "living-main-structural", "projection": "equirectangular", "scenes": scenes,
    "share": {"title": "fp-001 客餐厅结构全景"},
}
pano_payload["contentHash"] = canonical_sha256(pano_payload)
viewer_manifest = PanoManifest.model_validate(pano_payload)
(PUBLIC / "structural-manifest.json").write_text(viewer_manifest.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
print("camera, cubemap, panorama manifests + structural viewer manifest")
