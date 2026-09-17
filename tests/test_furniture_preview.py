from types import SimpleNamespace

from pano_platform.contracts import PanoManifest
from scripts.build_furniture_preview import furniture_yaw, manifest_payload, scene_name


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


def test_furniture_yaw_points_from_camera_to_furniture() -> None:
    furniture = SimpleNamespace(items=[SimpleNamespace(
        roomId="living", center=SimpleNamespace(x=0, y=-10),
    )])
    cameras = [SimpleNamespace(
        roomId="living", position=SimpleNamespace(x=0, y=0),
    )]
    assert furniture_yaw("living", furniture, cameras) == 0.0
