"""Convert the pinned ResPlan vector release into local regression fixtures.

The upstream artifact contains a pickle.  This importer verifies the exact
artifact hash and uses an allow-list unpickler; it never calls plain
``pickle.load`` on third-party bytes.  Derived files stay under ``tmp/`` and
are intentionally not committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
from shapely import affinity
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "datasets" / "external" / "registry.json"
DEFAULT_ZIP = ROOT / "tmp" / "external" / "resplan" / "ResPlan.zip"
DEFAULT_SPLIT = ROOT / "tmp" / "external" / "resplan" / "split.json"
DEFAULT_OUT = ROOT / "tmp" / "external" / "resplan" / "derived"
CONVERTER_VERSION = "v3-window-parallel-lines"

ROOM_TYPES = {
    "living": "living",
    "bedroom": "bedroom",
    "bathroom": "bathroom",
    "kitchen": "kitchen",
    "balcony": "balcony",
    "storage": "other",
    "stair": "other",
}


class RestrictedUnpickler(pickle.Unpickler):
    """Allow only the constructors used by the pinned geometry release."""

    ALLOWED = {
        ("shapely.io", "from_wkb"),
        ("numpy", "dtype"),
        ("numpy._core.multiarray", "scalar"),
        ("numpy.core.multiarray", "scalar"),
    }

    def find_class(self, module: str, name: str):
        if (module, name) not in self.ALLOWED:
            raise pickle.UnpicklingError(f"blocked pickle global: {module}.{name}")
        return super().find_class(module, name)


def _dataset_record() -> dict:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return next(item for item in registry["datasets"] if item["id"] == "resplan")


def verify_artifact(path: Path) -> dict:
    record = _dataset_record()
    if path.stat().st_size != record["artifactBytes"]:
        raise ValueError(f"ResPlan.zip size mismatch: {path.stat().st_size}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != record["artifactSha256"]:
        raise ValueError(f"ResPlan.zip SHA-256 mismatch: {digest}")
    return record


def load_plans(path: Path) -> list[dict]:
    verify_artifact(path)
    with zipfile.ZipFile(path) as archive:
        if archive.namelist() != ["ResPlan.pkl"]:
            raise ValueError(f"unexpected ResPlan archive entries: {archive.namelist()}")
        with archive.open("ResPlan.pkl") as source:
            plans = RestrictedUnpickler(source).load()
    if not isinstance(plans, list) or not all(isinstance(plan, dict) for plan in plans):
        raise ValueError("unexpected ResPlan root object")
    return plans


def geometries(value) -> list:
    if value is None or value.is_empty:
        return []
    if isinstance(value, (Polygon, LineString, Point)):
        return [value]
    if isinstance(value, (MultiPolygon, MultiLineString, GeometryCollection)):
        return [part for part in value.geoms if not part.is_empty]
    return []


def _transform(plan: dict, canvas: int, margin: int):
    candidates = [plan.get(key) for key in (*ROOM_TYPES, "wall", "door", "window", "front_door", "inner")]
    merged = unary_union([item for item in candidates if item is not None and not item.is_empty])
    min_x, min_y, max_x, max_y = merged.bounds
    scale = (canvas - 2 * margin) / max(max_x - min_x, max_y - min_y)

    def apply(geom):
        moved = affinity.translate(geom, xoff=-min_x, yoff=-max_y)
        scaled = affinity.scale(moved, xfact=scale, yfact=-scale, origin=(0, 0))
        return affinity.translate(scaled, xoff=margin, yoff=margin)

    return apply


def _polygon_points(geom: Polygon) -> list[dict]:
    return [{"x": round(x, 2), "y": round(y, 2)} for x, y in list(geom.exterior.coords)[:-1]]


def _long_axis(geom) -> tuple[dict, dict]:
    rectangle = geom.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords)[:4]
    edges = [(coords[index], coords[(index + 1) % 4]) for index in range(4)]
    start, end = max(edges, key=lambda edge: (edge[1][0] - edge[0][0]) ** 2 + (edge[1][1] - edge[0][1]) ** 2)
    opposite = edges[(edges.index((start, end)) + 2) % 4]
    return (
        {"x": round((start[0] + opposite[1][0]) / 2, 2), "y": round((start[1] + opposite[1][1]) / 2, 2)},
        {"x": round((end[0] + opposite[0][0]) / 2, 2), "y": round((end[1] + opposite[0][1]) / 2, 2)},
    )


def _draw_geometry(image: np.ndarray, geom, color: int) -> None:
    for part in geometries(geom):
        if not isinstance(part, Polygon):
            continue
        exterior = np.rint(np.asarray(part.exterior.coords)).astype(np.int32)
        cv2.fillPoly(image, [exterior], color)
        for ring in part.interiors:
            cv2.fillPoly(image, [np.rint(np.asarray(ring.coords)).astype(np.int32)], 255 - color)


def _draw_door_symbol(image: np.ndarray, start: dict, end: dict) -> None:
    """Draw one conventional leaf + quarter arc from authoritative geometry."""

    hinge = np.array([start["x"], start["y"]], dtype=float)
    along = np.array([end["x"] - start["x"], end["y"] - start["y"]], dtype=float)
    radius = float(np.linalg.norm(along))
    if radius < 4:
        return
    perpendicular = np.array([-along[1], along[0]]) / radius
    leaf_end = hinge + perpendicular * radius
    center = tuple(np.rint(hinge).astype(int))
    cv2.line(image, center, tuple(np.rint(leaf_end).astype(int)), 0, 2, cv2.LINE_AA)
    start_angle = math.degrees(math.atan2(along[1], along[0]))
    cv2.ellipse(image, center, (round(radius), round(radius)), 0, start_angle, start_angle + 90, 0, 2, cv2.LINE_AA)


def _draw_window_symbol(image: np.ndarray, start: dict, end: dict) -> None:
    """Draw the two parallel pane lines expected by the window detector."""

    first = np.array([start["x"], start["y"]], dtype=float)
    second = np.array([end["x"], end["y"]], dtype=float)
    along = second - first
    length = float(np.linalg.norm(along))
    if length < 4:
        return
    normal = np.array([-along[1], along[0]]) / length * 3.0
    for offset in (-normal, normal):
        cv2.line(
            image,
            tuple(np.rint(first + offset).astype(int)),
            tuple(np.rint(second + offset).astype(int)),
            0, 2, cv2.LINE_AA,
        )


def convert_plan(plan: dict, output_dir: Path, canvas: int = 1024) -> dict:
    sample_id = f"resplan-{int(plan['id']):05d}"
    apply = _transform(plan, canvas, margin=48)
    image = np.full((canvas, canvas), 255, dtype=np.uint8)
    _draw_geometry(image, apply(plan["wall"]), 0)

    openings = []
    opening_index = 0
    for source_type, truth_type in (("door", "door"), ("front_door", "door"), ("window", "window")):
        for part in geometries(plan.get(source_type)):
            transformed = apply(part)
            _draw_geometry(image, transformed, 255)
            start, end = _long_axis(transformed)
            openings.append({
                "id": f"op_{opening_index:03d}", "type": truth_type,
                "pixelStart": start, "pixelEnd": end,
                "evidence": "none" if truth_type == "door" else "parallelLines",
            })
            opening_index += 1
            if truth_type == "door":
                _draw_door_symbol(image, start, end)
            else:
                _draw_window_symbol(image, start, end)

    rooms = []
    for source_type, truth_type in ROOM_TYPES.items():
        for part in geometries(plan.get(source_type)):
            if isinstance(part, Polygon):
                rooms.append({
                    "id": f"room_{len(rooms):03d}",
                    "type": truth_type,
                    "polygon": _polygon_points(apply(part)),
                })

    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / f"{sample_id}.png"
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"failed to encode {sample_id}")
    image_path.write_bytes(encoded.tobytes())
    truth = {
        "schemaVersion": "0.1.0",
        "sampleId": sample_id,
        "imageSha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "annotator": "ResPlan vector ground truth",
        "annotatedAt": datetime.now(timezone.utc).isoformat(),
        "notes": "CC BY 4.0; synthetic raster generated from ResPlan vector geometry.",
        "openings": openings,
        "rooms": rooms,
        "scales": [],
    }
    (output_dir / f"{sample_id}.truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"sampleId": sample_id, "rooms": len(rooms), "openings": len(openings)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare ResPlan regression truth")
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--split", type=Path, default=DEFAULT_SPLIT)
    parser.add_argument("--partition", choices=("train", "val", "test"), default="test")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    plans = load_plans(args.zip)
    split_ids = json.loads(args.split.read_text(encoding="utf-8"))[args.partition]
    by_id = {int(plan["id"]): plan for plan in plans}
    selected = [by_id[plan_id] for plan_id in split_ids[: args.limit]]
    samples = [convert_plan(plan, args.out) for plan in selected]
    manifest = {
        "dataset": "resplan",
        "sourceVersion": _dataset_record()["version"],
        "converterVersion": CONVERTER_VERSION,
        "partition": args.partition,
        "samples": samples,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
