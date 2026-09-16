import hashlib
import json
from datetime import datetime
from pathlib import Path

from pano_platform.contracts import ArtifactManifest, ArtifactType


ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "datasets" / "derived" / "fp-001"
folder = DERIVED / "control-maps"
index_path = folder / "control-maps.index.json"
index_bytes = index_path.read_bytes()
index = json.loads(index_bytes)
scene = json.loads((DERIVED / "scene-geometry.reviewed-v5.json").read_text(encoding="utf-8"))
manifest = ArtifactManifest(
    id="artifact:fp001:control-maps:r5:1024x768",
    projectId="fp001",
    artifactType=ArtifactType.CONTROL_MAP_SET,
    contentHash=hashlib.sha256(index_bytes).hexdigest(),
    byteLength=len(index_bytes),
    contentType="application/json",
    generator={"name": "three-control-map-capture", "version": "0.1.0"},
    inputDigests=[scene["contentHash"]],
    createdAt=datetime.fromisoformat(index["generatedAt"].replace("Z", "+00:00")),
    metadata={"indexFile": "control-maps.index.json", "assets": index["assets"]},
)
(folder / "control-maps.artifact-manifest.json").write_text(
    manifest.model_dump_json(indent=2), encoding="utf-8"
)
print(manifest.id)
