from pathlib import Path

from pano_platform.contracts import DerivedRoomSet, LayoutRevision
from pano_platform.scene_geometry import build_scene_geometry


ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "datasets" / "derived" / "fp-001"
layout = LayoutRevision.model_validate_json(
    (DERIVED / "layout-revision.reviewed-v3.json").read_text(encoding="utf-8")
)
rooms = DerivedRoomSet.model_validate_json((DERIVED / "rooms.manual-v2.json").read_text(encoding="utf-8"))
geometry = build_scene_geometry(layout, rooms)
(DERIVED / "scene-geometry.reviewed-v3.json").write_text(
    geometry.model_dump_json(indent=2), encoding="utf-8"
)
print(f"{len(geometry.solids)} solids -> scene-geometry.reviewed-v3.json")
