from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pano_platform.contracts import PanoManifest, canonical_sha256  # noqa: E402
from pano_platform.furniture import build_furniture_plan, load_furniture_inputs  # noqa: E402


def scene_name(room_id: str) -> str:
    return "living-main" if room_id == "room_living_dining" else f"{room_id.removeprefix('room_').replace('_', '-')}-main"


def furniture_yaw(room_id: str, furniture, cameras) -> float:
    items = [item for item in furniture.items if item.roomId == room_id]
    camera = next((value for value in cameras if value.roomId == room_id), None)
    if not items or camera is None:
        return 0.0
    center_x = sum(item.center.x for item in items) / len(items)
    center_y = sum(item.center.y for item in items) / len(items)
    world_angle = math.degrees(math.atan2(-(center_y - camera.position.y), center_x - camera.position.x))
    return round((world_angle - 90.0 + 180.0) % 360.0 - 180.0, 3)


def manifest_payload(project_id: str, folder: str, room_id: str, room_name: str, scene: str, asset_name: str, yaw: float) -> dict:
    url = f"/artifacts/{folder}/panorama-furniture/{scene}/{asset_name}"
    asset = {"url": url, "width": 2048, "height": 1024, "contentType": "image/png"}
    payload = {
        "schemaVersion": "0.1.0", "projectId": project_id, "revision": 1,
        "defaultSceneId": scene, "projection": "equirectangular",
        "scenes": [{
            "id": scene, "roomId": room_id, "roomName": room_name,
            "title": f"{room_name} 确定性家具预览", "order": 0,
            "assets": {"original": asset, "proxy4k": None, "proxy2k": asset, "proxy1k": asset, "thumbnailUrl": url},
            "initialView": {"yaw": yaw, "pitch": 0.0, "fov": 55.0}, "hotspots": [],
        }],
        "share": {"title": f"{project_id} · 确定性家具预览"},
    }
    payload["contentHash"] = canonical_sha256(payload)
    return payload


def atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="capture deterministic furniture panorama and publish a Viewer manifest")
    parser.add_argument("room", nargs="?", default="room_living_dining")
    parser.add_argument("--project", default="fp001")
    parser.add_argument("--project-folder")
    args = parser.parse_args()
    project_id = args.project
    folder = args.project_folder or ("fp-001" if project_id == "fp001" else project_id)
    derived = ROOT / "datasets" / "derived" / folder
    scene = scene_name(args.room)
    layout, rooms, cameras = load_furniture_inputs(project_id)
    furniture = build_furniture_plan(layout, rooms, cameras)
    room = next((value for value in rooms.rooms if value.id == args.room), None)
    if room is None:
        raise SystemExit(f"room {args.room} not found")
    env = dict(os.environ, PANO_PROJECT_ID=project_id, PANO_PROJECT_FOLDER=folder, PANO_FURNITURE="1")
    env["PYTHONPATH"] = str(ROOT / "backend")
    started = time.perf_counter()
    subprocess.run(["node", str(ROOT / "scripts/capture_fp001_cubemap.mjs"), args.room, "rgb", "512"], cwd=ROOT, env=env, check=True)
    captured = time.perf_counter()
    subprocess.run([sys.executable, str(ROOT / "scripts/cubemap_to_erp.py"), "--scene", scene, "--face-size", "512", "--width", "2048"], cwd=ROOT, env=env, check=True)
    composed = time.perf_counter()

    scene_folder = derived / "panorama-furniture" / scene
    index = json.loads((scene_folder / "panorama-furnished-reference.index.json").read_text(encoding="utf-8"))
    source = scene_folder / index["file"]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != index["sha256"] or index.get("furniturePlanHash") is None:
        raise SystemExit("furnished panorama verification failed; manifest not published")
    immutable = scene_folder / f"{scene}-furnished-{digest}.png"
    if not immutable.exists():
        temporary = immutable.with_suffix(".tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, immutable)

    yaw = furniture_yaw(args.room, furniture, cameras)
    manifest = PanoManifest.model_validate(manifest_payload(project_id, folder, args.room, room.name, scene, immutable.name, yaw))
    target = derived / "pano-furniture-manifest.json"
    atomic_write(target, manifest.model_dump_json(indent=2, exclude_none=True).encode("utf-8"))
    timing = {"captureSeconds": round(captured - started, 3), "composeSeconds": round(composed - captured, 3), "totalSeconds": round(composed - started, 3), "erpSha256": digest}
    atomic_write(derived / "pano-furniture-timing.json", json.dumps(timing, indent=2).encode("utf-8"))
    print(f"viewer: http://127.0.0.1:5173/?manifestUrl=/artifacts/{folder}/{target.name}")
    print(json.dumps(timing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
