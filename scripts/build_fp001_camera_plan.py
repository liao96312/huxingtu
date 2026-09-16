from pathlib import Path

from shapely.geometry import Polygon
from shapely.ops import polylabel

from pano_platform.contracts import CameraPlan, DerivedRoomSet, LayoutRevision, canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
DERIVED = ROOT / "datasets" / "derived" / "fp-001"
layout = LayoutRevision.model_validate_json((DERIVED / "layout-revision.reviewed-v5.json").read_text(encoding="utf-8"))
rooms = DerivedRoomSet.model_validate_json((DERIVED / "rooms.manual-v2.json").read_text(encoding="utf-8"))
cameras = []
for room in rooms.rooms:
    polygon = Polygon([(point.x, point.y) for point in room.polygon])
    point = polylabel(polygon, tolerance=10)
    cameras.append({
        "id": f"camera_{room.id}_main",
        "roomId": room.id,
        "position": {"x": round(point.x, 3), "y": round(point.y, 3), "z": 1500.0},
        "clearance": round(point.distance(polygon.boundary), 3),
        "method": "polylabel",
    })
payload = {
    "schemaVersion": "0.1.0", "projectId": "fp001", "layoutRevisionId": layout.id,
    "sourceLayoutHash": layout.contentHash, "sourceRoomHash": rooms.contentHash, "cameras": cameras,
}
payload["contentHash"] = canonical_sha256(payload)
plan = CameraPlan.model_validate(payload)
(DERIVED / "camera-plan.auto-v1.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
print(f"{len(plan.cameras)} cameras -> camera-plan.auto-v1.json")
