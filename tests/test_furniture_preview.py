from types import SimpleNamespace

from pano_platform.contracts import PanoManifest
from scripts.build_furniture_preview import furniture_yaw, manifest_payload, scene_name, stable_manifest
from scripts.build_furniture_preview_batch import combined_manifest


def test_furniture_manifest_references_only_immutable_furnished_asset() -> None:
    assert scene_name("vlm_room_006") == "vlm-room-006-main"
    name = "vlm-room-006-main-furnished-" + "a" * 64 + ".png"
    manifest = PanoManifest.model_validate(manifest_payload(
        "p0-00-three-room-v1", "p0-00-three-room-v1", "vlm_room_006", "客餐厅",
        "vlm-room-006-main", name, 179.9,
    ))
    scene = manifest.scenes[0]
    assert scene.assets.original.url.endswith(name)
    assert "/panorama-furniture/" in scene.assets.original.url
    assert scene.initialView.yaw == 179.9
    assert scene.initialView.pitch == -12.0


def test_furniture_yaw_points_from_camera_to_furniture() -> None:
    furniture = SimpleNamespace(items=[SimpleNamespace(
        roomId="living", center=SimpleNamespace(x=0, y=-10),
    )])
    cameras = [SimpleNamespace(
        roomId="living", position=SimpleNamespace(x=0, y=0),
    )]
    assert furniture_yaw("living", furniture, cameras) == 0.0


def test_combined_manifest_keeps_all_scenes() -> None:
    first = PanoManifest.model_validate(manifest_payload(
        "p", "p", "room_1", "客厅", "room-1-main", "asset.png", 0,
    )).scenes[0].model_dump(mode="json", exclude_none=True)
    second = {**first, "id": "room-2-main", "roomId": "room_2", "order": 1}
    manifest = combined_manifest("p", [first, second], "room-1-main")
    assert [scene.id for scene in manifest.scenes] == ["room-1-main", "room-2-main"]


def test_furniture_manifest_round_trips_without_null_assets() -> None:
    manifest = stable_manifest(manifest_payload("p", "p", "room", "房间", "room-main", "asset.png", 0))
    assert PanoManifest.model_validate_json(manifest.model_dump_json(exclude_none=True)) == manifest
