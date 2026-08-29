"""Run the T08 parse pipeline on a single floorplan image and print the audit.

Reads an image path, runs CV + (optional) Tuzi vision in the requested mode, and
prints a per-stage trace with the final status. Never publishes a wrong revision;
a non-accepted run is reported as ``NEEDS_REVIEW``.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pano_platform.floorplan.parse import VisionMode, orchestrate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the parse pipeline on one floorplan")
    parser.add_argument("image", help="path to a floorplan image")
    parser.add_argument("--mode", choices=["off", "shadow", "assisted"], default="assisted")
    parser.add_argument("--prompt-version", default="floorplan_parse_v1")
    parser.add_argument(
        "--allow-external-vision", action="store_true",
        help="confirm that this image may be sent to Tuzi/upstream vision",
    )
    parser.add_argument("--out", default=None, help="write the trace JSON here")
    args = parser.parse_args()

    mode = {"off": VisionMode.OFF, "shadow": VisionMode.SHADOW, "assisted": VisionMode.ASSISTED}[args.mode]

    image_bytes = Path(args.image).read_bytes()
    vision_config = None
    if mode is not VisionMode.OFF and args.allow_external_vision:
        try:
            from pano_platform.vision.config import VisionConfig

            vision_config = VisionConfig.from_env()
        except ValueError as error:
            print(f"[config] {error}; running CV-only", file=sys.stderr)
    elif mode is not VisionMode.OFF:
        print("[privacy] external vision not authorised; running CV-only", file=sys.stderr)

    run = orchestrate(
        image_bytes,
        vision_mode=mode,
        vision_config=vision_config,
        prompt_version=args.prompt_version,
    )

    print(f"[run] trace={run.traceId} status={run.status.value} mode={run.visionMode.value} dirty={run.dirty}")
    for stage in run.stages:
        print(f"  [{stage.stage}] {stage.status} :: {stage.detail}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "traceId": run.traceId,
            "status": run.status.value,
            "visionMode": run.visionMode.value,
            "roomsPublished": run.rooms_published,
            "openingsCandidatesPublished": run.openings_candidates_published,
            "stages": [{"stage": s.stage, "status": s.status, "detail": s.detail} for s in run.stages],
            "reasonCodes": list(run.reasonCodes),
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[write] {args.out}")

    return 0 if run.status.value in ("accepted",) else 1


if __name__ == "__main__":
    raise SystemExit(main())
