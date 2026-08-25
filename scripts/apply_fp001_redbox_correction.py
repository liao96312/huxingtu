import json
from pathlib import Path

from pano_platform.contracts import DerivedRoomSet, OpeningType, Point2D, canonical_sha256
from pano_platform.manual_correction import add_pixel_annotated_opening
from pano_platform.opening_review import load_latest_reviewed_layout
from pano_platform.scene_geometry import build_scene_geometry


ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "datasets" / "derived" / "fp-001"
source = load_latest_reviewed_layout("fp001")
if source.revision >= 4:
    raise SystemExit("red-box correction is already applied")

corrected = add_pixel_annotated_opening(
    source,
    opening_id="opening_manual_redbox_02",
    opening_type=OpeningType.DOOR,
    pixel_start=Point2D(x=433, y=203),
    pixel_end=Point2D(x=433, y=232),
)
(DERIVED / "layout-revision.reviewed-v4.json").write_text(
    corrected.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
)

audit = {
    "schemaVersion": "0.1.0",
    "projectId": "fp001",
    "sourceLayoutHash": source.contentHash,
    "annotation": {
        "source": "user-red-box",
        "pixelStart": {"x": 433, "y": 203},
        "pixelEnd": {"x": 433, "y": 232},
        "type": "door",
    },
    "openingId": "opening_manual_redbox_02",
    "outputLayoutHash": corrected.contentHash,
}
audit["contentHash"] = canonical_sha256(audit)
(DERIVED / "manual-correction.v4.json").write_text(
    json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
)

rooms = DerivedRoomSet.model_validate_json((DERIVED / "rooms.manual-v2.json").read_text(encoding="utf-8"))
geometry = build_scene_geometry(corrected, rooms)
(DERIVED / "scene-geometry.reviewed-v4.json").write_text(
    geometry.model_dump_json(indent=2), encoding="utf-8"
)
print(f"{corrected.id}: {len(corrected.openings)} openings, {len(geometry.solids)} solids")
