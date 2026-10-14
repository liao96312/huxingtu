"""Pre-render ResPlan plans into a segmentation training set.

Images are rendered with ``prepare_resplan_truth.render_plan`` (byte-identical
to the regression fixtures) and downsampled to ``--size``. Labels are one PNG
per image: low 3 bits = class (0 background/exterior, 1 wall, 2 door,
3 window, 4 room interior), bit 3 = room-boundary (outline of every room
polygon, so open-plan rooms without a wall still get a separating line).

Output layout (zip it and ship it; nothing here goes into git):
    <out>/train/img/*.png  <out>/train/lbl/*.png
    <out>/val/img/*.png    <out>/val/lbl/*.png
    <out>/manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prepare_resplan_truth import (  # noqa: E402
    DEFAULT_SPLIT, DEFAULT_ZIP, geometries, load_plans, render_plan,
)
from shapely.geometry import Polygon  # noqa: E402

CANVAS = 1024
CLASSES = ("background", "wall", "door", "window", "room")
BOUNDARY_PX_AT_CANVAS = 6  # ~3 px at 512


def _fill(label: np.ndarray, geoms, value: int) -> None:
    for geom in geoms:
        for part in geometries(geom):
            if isinstance(part, Polygon):
                cv2.fillPoly(label, [np.rint(np.asarray(part.exterior.coords)).astype(np.int32)], value)
                for ring in part.interiors:
                    cv2.fillPoly(label, [np.rint(np.asarray(ring.coords)).astype(np.int32)], 0)


def render_sample(plan: dict, size: int):
    image, _, _, geometry = render_plan(plan, CANVAS, with_geometry=True)
    label = np.zeros((CANVAS, CANVAS), np.uint8)
    _fill(label, geometry["rooms"], 4)
    _fill(label, [geometry["wall"]], 1)
    _fill(label, geometry["door"], 2)
    _fill(label, geometry["window"], 3)
    boundary = np.zeros((CANVAS, CANVAS), np.uint8)
    for room in geometry["rooms"]:
        cv2.polylines(boundary, [np.rint(np.asarray(room.exterior.coords)).astype(np.int32)],
                      True, 1, BOUNDARY_PX_AT_CANVAS)
    image = cv2.resize(image, (size, size), interpolation=cv2.INTER_AREA)
    label = cv2.resize(label, (size, size), interpolation=cv2.INTER_NEAREST)
    boundary = cv2.resize(boundary, (size, size), interpolation=cv2.INTER_NEAREST)
    return image, (label | (boundary << 3)).astype(np.uint8)


def _work(args):
    plan, size, out = args
    sample_id = f"resplan-{int(plan['id']):05d}"
    try:
        image, label = render_sample(plan, size)
    except Exception:  # a few plans have degenerate opening geometry
        return None
    cv2.imwrite(str(out / "img" / f"{sample_id}.png"), image, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    cv2.imwrite(str(out / "lbl" / f"{sample_id}.png"), label, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    return sample_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--split", type=Path, default=DEFAULT_SPLIT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--train", type=int, default=0, help="0 = whole train split")
    parser.add_argument("--val", type=int, default=400)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    plans = {int(plan["id"]): plan for plan in load_plans(args.zip)}
    split = json.loads(args.split.read_text(encoding="utf-8"))
    # The regression slices are the first 30 test / first 40 val ids; the
    # test split is never rendered here, and val samples are taken from the
    # END of the val split so the 40 tuning images stay unseen.
    parts = {
        "train": split["train"][: args.train or None],
        "val": split["val"][-args.val:],
    }
    counts = {}
    for name, ids in parts.items():
        out = args.out / name
        (out / "img").mkdir(parents=True, exist_ok=True)
        (out / "lbl").mkdir(parents=True, exist_ok=True)
        jobs = [(plans[i], args.size, out) for i in ids if i in plans]
        with ProcessPoolExecutor(args.workers) as pool:
            written = 0
            for index, result in enumerate(pool.map(_work, jobs, chunksize=16), 1):
                written += result is not None
                if index % 1000 == 0:
                    print(name, index, flush=True)
        counts[name] = written
    (args.out / "manifest.json").write_text(json.dumps({
        "source": "ResPlan (CC BY 4.0), rendered by scripts/prepare_resplan_truth.render_plan",
        "size": args.size, "classes": list(CLASSES), "boundaryBit": 3, "counts": counts,
    }, indent=2), encoding="utf-8")
    print(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
