from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from pano_platform.contracts import PanoManifest, canonical_sha256  # noqa: E402
from pano_platform.furniture import load_furniture_inputs  # noqa: E402
from scripts.build_furniture_preview import atomic_write, stable_manifest  # noqa: E402


def combined_manifest(project_id: str, scenes: list[dict], default_scene_id: str) -> PanoManifest:
    payload = {
        "schemaVersion": "0.1.0", "projectId": project_id, "revision": 1,
        "defaultSceneId": default_scene_id, "projection": "equirectangular",
        "scenes": scenes, "share": {"title": f"{project_id} · 确定性家具全景"},
    }
    payload["contentHash"] = canonical_sha256(payload)
    return stable_manifest(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="build and stability-check every room panorama")
    parser.add_argument("--project", default="fp-101")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if args.rounds < 1:
        raise SystemExit("rounds must be positive")
    _, rooms, _ = load_furniture_inputs(args.project)
    ordered = sorted(rooms.rooms, key=lambda room: (room.type != "livingDining", room.id))
    folder = "fp-001" if args.project == "fp001" else args.project
    derived = ROOT / "datasets/derived" / folder
    manifest_path = derived / "pano-furniture-manifest.json"
    runs = []
    expected_assets = None
    started = time.perf_counter()
    for round_number in range(1, args.rounds + 1):
        scenes = []
        round_started = time.perf_counter()
        for room in ordered:
            subprocess.run([
                sys.executable, str(ROOT / "scripts/build_furniture_preview.py"), room.id,
                "--project", args.project,
            ], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
            scene = PanoManifest.model_validate_json(manifest_path.read_text(encoding="utf-8")).scenes[0]
            scene_payload = scene.model_dump(mode="json", exclude_none=True)
            scene_payload["order"] = len(scenes)
            scenes.append(scene_payload)
        assets = [scene["assets"]["original"]["url"] for scene in scenes]
        if expected_assets is not None and assets != expected_assets:
            raise SystemExit(f"round {round_number} produced unstable panorama hashes")
        expected_assets = assets
        runs.append({"round": round_number, "seconds": round(time.perf_counter() - round_started, 3), "assets": assets})
        manifest = combined_manifest(args.project, scenes, scenes[0]["id"])
        atomic_write(manifest_path, manifest.model_dump_json(indent=2, exclude_none=True).encode("utf-8"))
        print(f"round {round_number}/{args.rounds}: {len(scenes)} rooms, {runs[-1]['seconds']}s")
    report = {
        "schemaVersion": "0.1.0", "projectId": args.project, "rounds": runs,
        "roomCount": len(ordered), "stable": True, "totalSeconds": round(time.perf_counter() - started, 3),
    }
    report["contentHash"] = canonical_sha256(report)
    atomic_write(derived / "pano-furniture-batch-report.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"viewer: http://127.0.0.1:5173/?manifestUrl=/artifacts/{folder}/pano-furniture-manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
