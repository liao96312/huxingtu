import json
import hashlib
import struct
from pathlib import Path

import cv2

from pano_platform.contracts import ArtifactManifest, ArtifactType, PanoManifest


ROOT = Path(__file__).resolve().parents[1]
AI = ROOT / "datasets/derived/fp-001/panorama/living-main/ai"


def test_ai_panorama_is_normalized_and_auditable() -> None:
    record = json.loads((AI / "living-main-gpt-image-2-v1.request.json").read_text(encoding="utf-8"))
    output = ROOT / record["outputFile"]
    payload = output.read_bytes()
    assert payload[:2] == b"\xff\xd8"
    assert record["rawWidth"] == record["rawHeight"] * 2
    assert (record["outputWidth"], record["outputHeight"]) == (2048, 1024)
    assert record["outputSeamMae"] < 1
    assert record["status"] == "needsStructureReview"
    upscale = record["upscale"]
    assert [(asset["width"], asset["height"]) for asset in upscale["assets"]] == [
        (6144, 3072), (4096, 2048), (2048, 1024), (1024, 512)
    ]
    proxy4k = upscale["assets"][1]
    proxy4k_path = AI / proxy4k["file"]
    proxy4k_image = cv2.imread(str(proxy4k_path), cv2.IMREAD_COLOR)
    assert proxy4k_image.shape[:2] == (2048, 4096)
    assert hashlib.sha256(proxy4k_path.read_bytes()).hexdigest() == proxy4k["sha256"]
    assert upscale["outputSeamMae"] < 1
    manifest = ArtifactManifest.model_validate_json((AI / "panoramaResult.artifact-manifest.json").read_text(encoding="utf-8"))
    assert manifest.artifactType is ArtifactType.PANORAMA_RESULT
    viewer = PanoManifest.model_validate_json((ROOT / "apps/pano-viewer/public/demo/ai-manifest.json").read_text(encoding="utf-8"))
    assert viewer.defaultSceneId == "living-main-ai-v1"
    assert viewer.scenes[0].assets.original.width == 6144
    assert viewer.scenes[0].assets.proxy4k is not None
    assert viewer.scenes[0].assets.proxy4k.width == 4096
    assert viewer.scenes[0].assets.proxy1k.width == 1024
    assert [scene.id for scene in viewer.scenes] == [
        "living-main-ai-v1", "kitchen-main-ai-v1", "living-main-ai-cream-v1", "living-main-ai-imagen-clarity-v1"
    ]


def test_kitchen_ai_panorama_has_6k_asset_chain() -> None:
    kitchen = ROOT / "datasets/derived/fp-001/panorama/kitchen-main/ai"
    record = json.loads((kitchen / "kitchen-main-gpt-image-2-v1.request.json").read_text(encoding="utf-8"))
    assert record["rawWidth"] == record["rawHeight"] * 2
    assert record["outputSeamMae"] == 0
    assert [(asset["width"], asset["height"]) for asset in record["upscale"]["assets"]] == [
        (6144, 3072), (4096, 2048), (2048, 1024), (1024, 512)
    ]
    assert record["upscale"]["outputSeamMae"] == 0


def test_cream_style_panorama_records_style_context() -> None:
    folder = ROOT / "datasets/derived/fp-001/panorama/living-main/ai"
    record = json.loads((folder / "living-main-gpt-image-2-cream-v1.request.json").read_text(encoding="utf-8"))
    assert record["styleContextId"] == "cream"
    assert record["rawWidth"] == record["rawHeight"] * 2
    assert record["upscale"]["assets"][0]["width"] == 6144
    assert record["upscale"]["outputSeamMae"] == 0
