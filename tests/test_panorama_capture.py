import hashlib
import json
import struct
from pathlib import Path

from pano_platform.contracts import ArtifactManifest, ArtifactType, PanoManifest


ROOT = Path(__file__).resolve().parents[1]
PANO = ROOT / "datasets/derived/fp-001/panorama/living-main"


def png_size(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", payload[16:24])


def test_cubemap_and_erp_are_complete_and_aligned() -> None:
    cubemap = json.loads((PANO / "cubemap/cubemap.index.json").read_text(encoding="utf-8"))
    assert [asset["face"] for asset in cubemap["assets"]] == ["px", "nx", "py", "ny", "pz", "nz"]
    for asset in cubemap["assets"]:
        path = PANO / "cubemap" / asset["file"]
        assert png_size(path) == (1024, 1024)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"]

    panorama = json.loads((PANO / "panorama-reference.index.json").read_text(encoding="utf-8"))
    path = PANO / panorama["file"]
    assert png_size(path) == (2048, 1024)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == panorama["sha256"]
    assert panorama["seamMae"] < 5

    manifests = [ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8")) for path in PANO.glob("*.artifact-manifest.json")]
    assert {manifest.artifactType for manifest in manifests} == {
        ArtifactType.CAMERA_PLAN, ArtifactType.CUBEMAP_SET, ArtifactType.PANORAMA_REFERENCE
    }
    viewer = PanoManifest.model_validate_json(
        (ROOT / "apps/pano-viewer/public/demo/structural-manifest.json").read_text(encoding="utf-8")
    )
    assert viewer.defaultSceneId == "living-main-structural"
    assert len(viewer.scenes) == 5


def test_every_room_has_a_structural_erp() -> None:
    scenes = ("living-main", "kitchen-main", "bed-center-main", "bed-left-main", "bath-main")
    for scene in scenes:
        folder = ROOT / "datasets/derived/fp-001/panorama" / scene
        index = json.loads((folder / "panorama-reference.index.json").read_text(encoding="utf-8"))
        assert png_size(folder / index["file"]) == (2048, 1024)
        assert index["seamMae"] < 1


def test_living_room_has_auditable_4k_capture() -> None:
    cubemap = json.loads((PANO / "cubemap-2048/cubemap.index.json").read_text(encoding="utf-8"))
    assert cubemap["faceSize"] == 2048
    for asset in cubemap["assets"]:
        path = PANO / "cubemap-2048" / asset["file"]
        assert png_size(path) == (2048, 2048)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"]

    panorama = json.loads((PANO / "panorama-reference-4096x2048.index.json").read_text(encoding="utf-8"))
    path = PANO / panorama["file"]
    assert png_size(path) == (4096, 2048)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == panorama["sha256"]
    assert panorama["seamMae"] < 1

    assert ArtifactManifest.model_validate_json(
        (PANO / "cubemapSet-high.artifact-manifest.json").read_text(encoding="utf-8")
    ).metadata["faces"][0]["width"] == 2048
    assert ArtifactManifest.model_validate_json(
        (PANO / "panoramaReference-high.artifact-manifest.json").read_text(encoding="utf-8")
    ).metadata["width"] == 4096
