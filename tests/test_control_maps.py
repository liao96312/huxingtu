import hashlib
import json
import struct
from pathlib import Path

from pano_platform.contracts import ArtifactManifest, ArtifactType


ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "datasets/derived/fp-001/control-maps"


def test_control_map_capture_is_aligned_and_audited() -> None:
    index = json.loads((FOLDER / "control-maps.index.json").read_text(encoding="utf-8"))
    assert [asset["pass"] for asset in index["assets"]] == ["rgb", "edge", "normal", "depth", "semantic"]
    hashes = set()
    for asset in index["assets"]:
        payload = (FOLDER / asset["file"]).read_bytes()
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", payload[16:24]) == (1024, 768)
        assert hashlib.sha256(payload).hexdigest() == asset["sha256"]
        hashes.add(asset["sha256"])
    assert len(hashes) == 5

    manifest = ArtifactManifest.model_validate_json(
        (FOLDER / "control-maps.artifact-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.artifactType is ArtifactType.CONTROL_MAP_SET
    assert len(manifest.metadata["assets"]) == 5
