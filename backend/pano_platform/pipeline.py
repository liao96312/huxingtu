"""Single-machine upload -> parse -> geometry -> panorama preview pipeline.

This is the smallest vertical slice that makes the existing stages testable as
one product flow. Geometry that has not passed the release gate is stored as a
clearly named preview artifact; it is never promoted to a reviewed revision.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from shapely.geometry import Polygon
from shapely.ops import polylabel

from .contracts import (
    DerivedRoomSet,
    LayoutRevision,
    PanoManifest,
    SceneGeometry,
    canonical_sha256,
)
from .floorplan.parse import ParseRun, ParseStatus, VisionMode, orchestrate
from .scene_geometry import build_scene_geometry

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = ROOT / "datasets" / "derived"
ERP_WIDTH = 2048
WALL_HEIGHT_MM = 2800.0


class PipelineNotReady(ValueError):
    pass


def run_upload_pipeline(
    image_bytes: bytes,
    *,
    project_id: str,
    allow_external_vision: bool,
    generate_ai: bool = True,
) -> dict[str, Any]:
    """Run the vertical slice synchronously and return user-facing evidence."""

    config = None
    if allow_external_vision:
        from .vision.config import VisionConfig

        config = VisionConfig.from_env()

    run = orchestrate(
        image_bytes,
        project_id=project_id,
        vision_mode=VisionMode.ASSISTED if allow_external_vision else VisionMode.OFF,
        vision_config=config,
    )
    result: dict[str, Any] = {
        "projectId": project_id,
        "traceId": run.traceId,
        "parseStatus": run.status.value,
        "reasonCodes": list(run.reasonCodes),
        "stages": [
            {"stage": stage.stage, "status": stage.status, "detail": stage.detail}
            for stage in run.stages
        ],
        "viewerUrl": None,
        "manifestUrl": None,
        "aiGenerated": False,
    }

    try:
        layout, rooms, scene = _publish_preview(project_id, image_bytes, run)
        manifest, ai_generated_count, ai_failed_rooms, superres_count, superres_failed_rooms = _render_panoramas(
            project_id, layout, rooms, generate_ai=generate_ai
        )
    except PipelineNotReady as error:
        result["pipelineStatus"] = "needsReview"
        result["message"] = str(error)
        return result

    manifest_url = f"/artifacts/{project_id}/pano-manifest.json"
    result.update({
        "pipelineStatus": "previewReady" if run.status is not ParseStatus.ACCEPTED else "ready",
        "layoutRevisionId": layout.id,
        "roomCount": len(rooms.rooms),
        "wallSolidCount": len(scene.solids),
        "manifestUrl": manifest_url,
        "viewerUrl": f"/?manifestUrl={manifest_url}",
        "aiGenerated": ai_generated_count > 0,
        "aiGeneratedCount": ai_generated_count,
        "aiFailedRooms": ai_failed_rooms,
        "superresGeneratedCount": superres_count,
        "superresFailedRooms": superres_failed_rooms,
    })
    warnings = []
    if ai_failed_rooms:
        warnings.append(f"以下房间AI失败，已回退结构全景：{', '.join(ai_failed_rooms)}")
    if superres_failed_rooms:
        warnings.append(f"以下房间本地超分失败，保留AI原图：{', '.join(superres_failed_rooms)}")
    if warnings:
        result["warning"] = "；".join(warnings)
    return result


def _publish_preview(
    project_id: str, image_bytes: bytes, run: ParseRun
) -> tuple[LayoutRevision, DerivedRoomSet, SceneGeometry]:
    fusion = next((stage for stage in run.stages if stage.stage == "fusion"), None)
    cv_stage = next((stage for stage in run.stages if stage.stage == "cv_structure"), None)
    if fusion is None:
        # CV-only fallback keeps the upload flow useful when Tuzi is down. It
        # is always a preview with generic room labels, never a confirmed layout.
        if cv_stage is None:
            raise PipelineNotReady("视觉草案未进入几何融合，当前没有可生成全景的房间")
        graph = cv_stage.data.get("wallGraph")
        if graph is None:
            raise PipelineNotReady("CV墙图不可用，当前没有可生成全景的房间")
        from .floorplan.fusion import FusedRoom
        from .floorplan.rooms import polygonize_rooms

        cv_rooms = polygonize_rooms(
            graph, float(cv_stage.data.get("wallWidthPx") or 0),
            (int(cv_stage.data.get("imageHeight") or 0), int(cv_stage.data.get("imageWidth") or 0)),
        )
        closed_rooms = tuple(
            FusedRoom(
                room.id, "other", room.polygon, room.area_mm2,
                room.area_px2, "review", ("CV_ONLY_ROOM",),
            )
            for room in cv_rooms
        )
        confirmed_openings = ()
    else:
        graph = fusion.data.get("wallGraph")
        closed_rooms = tuple(fusion.data.get("fusedRooms") or ())
        confirmed_openings = fusion.data.get("confirmedOpenings") or ()
    if graph is None or len(graph.segments) < 3 or not closed_rooms:
        raise PipelineNotReady("墙图或闭合房间不足，结果保留为待复核")

    source_data = fusion.data if fusion is not None else cv_stage.data
    mm_per_px = float(source_data.get("mmPerPx") or graph.mm_per_px or 0)
    if not math.isfinite(mm_per_px) or mm_per_px <= 0:
        raise PipelineNotReady("比例尺不可用，不能构建3D场景")

    node_ids: dict[tuple[float, float], str] = {}
    nodes: list[dict] = []

    def node_id(point: tuple[float, float]) -> str:
        key = (round(point[0], 3), round(point[1], 3))
        if key not in node_ids:
            identifier = f"node_{len(node_ids) + 1:03d}"
            node_ids[key] = identifier
            nodes.append({"id": identifier, "x": key[0] * mm_per_px, "y": key[1] * mm_per_px})
        return node_ids[key]

    edges = [
        {
            "id": segment.id,
            "startNodeId": node_id(segment.start),
            "endNodeId": node_id(segment.end),
            "thickness": min(300.0, max(80.0, segment.thickness_px * mm_per_px)),
            "height": WALL_HEIGHT_MM,
            "confidence": 0.5,
        }
        for segment in graph.segments
        if segment.start != segment.end
    ]
    if len(nodes) < 3 or len(edges) < 3:
        raise PipelineNotReady("有效墙段少于3条，不能构建3D场景")

    segments = {segment.id: segment for segment in graph.segments}
    openings = []
    for item in confirmed_openings:
        segment = segments.get(item.wall_edge_id)
        if segment is None or item.width_mm is None:
            continue
        start_offset = _distance(segment.start, item.start_px) * mm_per_px
        end_offset = _distance(segment.start, item.end_px) * mm_per_px
        offset, width = min(start_offset, end_offset), abs(end_offset - start_offset)
        edge_length = _distance(segment.start, segment.end) * mm_per_px
        if width <= 1 or offset + width > edge_length + 1:
            continue
        is_window = item.type == "window"
        openings.append({
            "id": item.id,
            "type": "window" if is_window else "door",
            "wallEdgeId": item.wall_edge_id,
            "offset": offset,
            "width": width,
            "height": 1500.0 if is_window else 2200.0,
            "sill": 600.0 if is_window else None,
            "confidence": 0.5,
        })

    room_annotations = []
    for room in closed_rooms:
        polygon = Polygon(room.polygon_px)
        anchor = polygon.representative_point()
        room_annotations.append({
            "id": f"annotation_{room.id}", "type": room.type,
            "label": room.type,
            "anchor": {"x": anchor.x * mm_per_px, "y": anchor.y * mm_per_px},
            "confidence": 0.5,
        })

    transform = (mm_per_px, 0.0, 0.0, 0.0, mm_per_px, 0.0, 0.0, 0.0, 1.0)
    layout_payload = {
        "schemaVersion": "0.1.0", "id": f"{project_id}_preview_r1",
        "projectId": project_id, "revision": 1, "units": "mm",
        "coordinateFrame": "business-z-up-rh",
        "source": {
            "imageArtifactId": f"sha256:{hashlib.sha256(image_bytes).hexdigest()}",
            "pixelToWorldTransform": transform, "scaleConfidence": 0.5,
        },
        "nodes": nodes, "edges": edges, "openings": openings,
        "roomAnnotations": room_annotations,
    }
    layout_payload["contentHash"] = canonical_sha256(layout_payload)
    layout = LayoutRevision.model_validate(layout_payload)

    room_items = []
    for room in closed_rooms:
        points = [{"x": x * mm_per_px, "y": y * mm_per_px} for x, y in room.polygon_px]
        area = abs(Polygon([(point["x"], point["y"]) for point in points]).area)
        if area <= 0:
            continue
        room_items.append({
            "id": room.id, "name": _room_name(room.type), "type": room.type,
            "polygon": points, "areaMm2": area, "confidence": 0.5,
        })
    if not room_items:
        raise PipelineNotReady("没有有效房间面，不能放置全景相机")
    rooms_payload = {
        "schemaVersion": "0.1.0", "projectId": project_id,
        "layoutRevisionId": layout.id, "sourceLayoutHash": layout.contentHash,
        "rooms": room_items,
    }
    rooms_payload["contentHash"] = canonical_sha256(rooms_payload)
    rooms = DerivedRoomSet.model_validate(rooms_payload)
    scene = build_scene_geometry(layout, rooms)

    folder = _data_root() / project_id
    _write_model(folder / "layout-revision.preview-v1.json", layout)
    _write_model(folder / "rooms.preview-v1.json", rooms)
    _write_model(folder / "scene-geometry.preview-v1.json", scene)
    return layout, rooms, scene


def _render_panoramas(
    project_id: str,
    layout: LayoutRevision,
    rooms: DerivedRoomSet,
    *,
    generate_ai: bool,
) -> tuple[PanoManifest, int, list[str], int, list[str]]:
    scenes = []
    ai_generated_count = 0
    ai_failed_rooms: list[str] = []
    superres_count = 0
    superres_failed_rooms: list[str] = []
    ordered_rooms = sorted(rooms.rooms, key=lambda room: room.areaMm2, reverse=True)
    for order, room in enumerate(ordered_rooms):
        scene_id = f"{room.id}-main"
        folder = _data_root() / project_id / "panorama" / scene_id
        folder.mkdir(parents=True, exist_ok=True)
        structural = folder / "structural-erp-2048x1024.jpg"
        image = render_structural_erp(layout, room.model_dump(mode="python"), ERP_WIDTH)
        cv2.imwrite(str(structural), image, [cv2.IMWRITE_JPEG_QUALITY, 94])

        original = structural
        superres_paths: dict[str, Path] | None = None
        ai_ready = False
        if generate_ai:
            try:
                original = _generate_ai_panorama(
                    structural, folder / "ai-erp-2048x1024.jpg", room.type,
                    _opening_hint(layout, room),
                )
                ai_generated_count += 1
                ai_ready = True
            except Exception:  # structural preview remains deliverable
                ai_failed_rooms.append(room.name)

        if ai_ready:
            try:
                superres_paths = _upscale_with_realesrgan(original, folder)
                original = superres_paths["original"]
                superres_count += 1
            except Exception:
                superres_failed_rooms.append(room.name)

        proxy = folder / "proxy-1024x512.jpg"
        thumb = folder / "thumb-320x160.jpg"
        loaded = cv2.imread(str(original), cv2.IMREAD_COLOR)
        asset_4k = superres_paths["proxy4k"] if superres_paths else None
        asset_2k = superres_paths["proxy2k"] if superres_paths else original
        asset_1k = superres_paths["proxy1k"] if superres_paths else proxy
        if not superres_paths:
            cv2.imwrite(str(proxy), cv2.resize(loaded, (1024, 512), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 90])
        cv2.imwrite(str(thumb), cv2.resize(loaded, (320, 160), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 85])
        base = f"/artifacts/{project_id}/panorama/{scene_id}"
        original_name = original.name
        scenes.append({
            "id": scene_id, "roomId": room.id, "roomName": room.name,
            "title": f"{room.name}{' AI+超分预览' if superres_paths else ' AI预览' if ai_ready else ' 结构预览'}",
            "order": order,
            "assets": {
                "original": {"url": f"{base}/{original_name}", "width": 6144 if superres_paths else 2048, "height": 3072 if superres_paths else 1024, "contentType": "image/jpeg"},
                "proxy4k": {"url": f"{base}/{asset_4k.name}", "width": 4096, "height": 2048, "contentType": "image/jpeg"} if asset_4k else None,
                "proxy2k": {"url": f"{base}/{asset_2k.name}", "width": 2048, "height": 1024, "contentType": "image/jpeg"},
                "proxy1k": {"url": f"{base}/{asset_1k.name}", "width": 1024, "height": 512, "contentType": "image/jpeg"},
                "thumbnailUrl": f"{base}/{thumb.name}",
            },
            "initialView": {"yaw": 0.0, "pitch": 0.0, "fov": 82.0},
        })

    payload = {
        "schemaVersion": "0.1.0", "projectId": project_id, "revision": 1,
        "defaultSceneId": scenes[0]["id"], "projection": "equirectangular",
        "scenes": scenes, "share": {"title": f"{project_id} 户型识别全景预览"},
    }
    payload["contentHash"] = canonical_sha256(payload)
    manifest = PanoManifest.model_validate(payload)
    _write_model(_data_root() / project_id / "pano-manifest.json", manifest)
    return manifest, ai_generated_count, ai_failed_rooms, superres_count, superres_failed_rooms


def _upscale_with_realesrgan(source: Path, folder: Path) -> dict[str, Path]:
    """Run the existing wrap-aware Real-ESRGAN tile script for one ERP."""

    tool = _resolve_realesrgan_tool()
    base = "ai-erp"
    command = [
        sys.executable, str(ROOT / "scripts" / "upscale_ai_panorama.py"),
        "--source", str(source), "--output-dir", str(folder),
        "--output-base", base, "--tool", str(tool),
    ]
    child_env = os.environ.copy()
    backend_path = str(ROOT / "backend")
    child_env["PYTHONPATH"] = backend_path + os.pathsep + child_env.get("PYTHONPATH", "")
    completed = subprocess.run(
        command, cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=900,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "Real-ESRGAN failed").strip()
        raise RuntimeError(detail[-1000:])
    paths = {
        "original": folder / f"{base}-6144x3072.jpg",
        "proxy4k": folder / f"{base}-proxy-4096x2048.jpg",
        "proxy2k": folder / f"{base}-proxy-2048x1024.jpg",
        "proxy1k": folder / f"{base}-proxy-1024x512.jpg",
    }
    if not all(path.is_file() for path in paths.values()):
        raise RuntimeError("Real-ESRGAN completed without all expected ERP assets")
    return paths


def _resolve_realesrgan_tool() -> Path:
    configured = os.environ.get("REALESRGAN_TOOL")
    candidates = [
        Path(configured) if configured else None,
        ROOT / "tools/realesrgan/package/realesrgan-ncnn-vulkan.exe",
        ROOT / "tmp/tools/realesrgan/realesrgan-ncnn-vulkan.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("Real-ESRGAN executable not found")


def render_structural_erp(layout: LayoutRevision, room: dict, width: int = ERP_WIDTH) -> np.ndarray:
    """Render a deterministic 2:1 ERP from vector walls with door/window holes."""

    height = width // 2
    polygon = Polygon([(point["x"], point["y"]) for point in room["polygon"]])
    camera = polylabel(polygon, tolerance=10)
    cx, cy, cz = float(camera.x), float(camera.y), 1500.0
    nodes = {node.id: node for node in layout.nodes}
    edges = layout.edges
    angles = (np.arange(width, dtype=np.float64) / width) * (2 * np.pi)
    directions = np.stack((np.cos(angles), np.sin(angles)), axis=1)
    distances = np.full((width, len(edges)), np.inf, dtype=np.float64)
    alongs = np.zeros_like(distances)

    for index, edge in enumerate(edges):
        a, b = nodes[edge.startNodeId], nodes[edge.endNodeId]
        hit_t, hit_u = _ray_segment_intersections(cx, cy, directions, a.x, a.y, b.x, b.y)
        valid = (hit_t > 1e-3) & (hit_u >= 0) & (hit_u <= 1)
        distances[valid, index] = hit_t[valid]
        alongs[valid, index] = hit_u[valid] * math.hypot(b.x - a.x, b.y - a.y)

    order = np.argsort(distances, axis=1)
    pitch = np.pi / 2 - ((np.arange(height, dtype=np.float64) + 0.5) / height) * np.pi
    slope = np.tan(pitch)[:, None]
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = (226, 232, 238)
    unset = np.ones((height, width), dtype=bool)
    openings_by_edge: dict[str, list] = {}
    for opening in layout.openings:
        openings_by_edge.setdefault(opening.wallEdgeId, []).append(opening)

    columns = np.arange(width)
    for rank in range(len(edges)):
        edge_indices = order[:, rank]
        distance = distances[columns, edge_indices]
        finite = np.isfinite(distance)
        if not finite.any():
            continue
        z = cz + slope * distance[None, :]
        solid = finite[None, :] & (z >= 0) & (z <= WALL_HEIGHT_MM)
        for edge_index, edge in enumerate(edges):
            selected = edge_indices == edge_index
            if not selected.any():
                continue
            along = alongs[columns, edge_indices]
            for opening in openings_by_edge.get(edge.id, []):
                inside_width = selected & (along >= opening.offset) & (along <= opening.offset + opening.width)
                sill = opening.sill or 0.0
                hole = inside_width[None, :] & (z >= sill) & (z <= sill + opening.height)
                solid &= ~hole
        paint = unset & solid
        if paint.any():
            shade = np.clip(245 - distance / 45, 145, 238).astype(np.uint8)
            wall_color = np.stack((shade - 8, shade - 3, shade), axis=1)
            image[paint] = np.broadcast_to(wall_color[None, :, :], image.shape)[paint]
            unset &= ~paint

    floor = unset & (pitch[:, None] < 0)
    ceiling = unset & ~floor
    image[floor] = (190, 205, 214)
    image[ceiling] = (238, 241, 244)
    # Horizontal reference bands make projection/room distortion easy to judge.
    for row in (height // 2, int(height * 0.72), int(height * 0.86)):
        image[max(0, row - 1):row + 1] = (150, 168, 180)
    return image


def _ray_segment_intersections(cx, cy, directions, ax, ay, bx, by):
    sx, sy = bx - ax, by - ay
    qx, qy = ax - cx, ay - cy
    denominator = directions[:, 0] * sy - directions[:, 1] * sx
    safe = np.where(np.abs(denominator) < 1e-12, np.nan, denominator)
    t = (qx * sy - qy * sx) / safe
    u = (qx * directions[:, 1] - qy * directions[:, 0]) / safe
    return t, u


def _generate_ai_panorama(
    source: Path, output: Path, room_type: str, opening_hint: str = ""
) -> Path:
    import base64
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    key = os.environ.get("TUZI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TUZI_API_KEY missing")
    prompt = (
        "Transform this exact structural 2:1 equirectangular 360 panorama into a photorealistic "
        f"warm contemporary {room_type} interior. Preserve every wall, architectural opening, camera position, "
        "projection and the left/right seam. This is a topology-locked edit, not a free redesign. "
        f"{opening_hint} Do not add or remove any architectural opening. Do not invent room doors, hallway doors, "
        "balcony doors, windows, wall cutouts, closets, wardrobes, cabinet doors, cupboard doors, handles, or "
        "door-shaped panels that are not present in the reference. If an opening is not specified, keep that wall "
        "blank. Use simple furniture with flat, handleless surfaces. Return a seamless 2:1 ERP."
    )
    idempotency_key = hashlib.sha256(
        source.read_bytes() + prompt.encode("utf-8") + b"gpt-image-2:2k"
    ).hexdigest()
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=4, connect=4, read=1, backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "POST")),
    )))
    with source.open("rb") as image:
        response = session.post(
            f"{os.environ.get('TUZI_BASE_URL', 'https://api.tu-zi.com').rstrip('/')}/v1/images/edits",
            headers={"Authorization": f"Bearer {key}", "Idempotency-Key": idempotency_key},
            data={"model": "gpt-image-2", "prompt": prompt, "n": "1", "size": "2048x1024", "quality": "2k", "response_format": "url"},
            files={"image": (source.name, image, "image/jpeg")}, timeout=(20, 300),
        )
    response.raise_for_status()
    document = response.json()
    items = document.get("data", {}).get("images", []) if isinstance(document.get("data"), dict) else document.get("data", [])
    if not items:
        raise RuntimeError("Tuzi image response has no image")
    item = items[0]
    if item.get("url"):
        downloaded = session.get(item["url"], timeout=(20, 180))
        downloaded.raise_for_status()
        payload = downloaded.content
    elif item.get("b64_json"):
        payload = base64.b64decode(item["b64_json"])
    else:
        raise RuntimeError("Tuzi image response has no usable payload")
    decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise RuntimeError("AI image decode failed")
    normalized = cv2.resize(decoded, (2048, 1024), interpolation=cv2.INTER_LANCZOS4)
    strip = 64
    for offset in range(strip):
        alpha = 0.5 * (1 + math.cos(math.pi * offset / strip))
        average = (normalized[:, offset].astype(np.float32) + normalized[:, -1 - offset].astype(np.float32)) / 2
        normalized[:, offset] = np.clip(normalized[:, offset] * (1 - alpha) + average * alpha, 0, 255)
        normalized[:, -1 - offset] = np.clip(normalized[:, -1 - offset] * (1 - alpha) + average * alpha, 0, 255)
    cv2.imwrite(str(output), normalized, [cv2.IMWRITE_JPEG_QUALITY, 94])
    return output


def _opening_hint(layout: LayoutRevision, room) -> str:
    """Describe only the allowed architectural openings for this room."""

    from shapely.geometry import Point

    polygon = Polygon([(point.x, point.y) for point in room.polygon])
    nodes = {node.id: node for node in layout.nodes}
    allowed = []
    for opening in layout.openings:
        edge = next((item for item in layout.edges if item.id == opening.wallEdgeId), None)
        if edge is None:
            continue
        start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
        ux, uy = end.x - start.x, end.y - start.y
        length = math.hypot(ux, uy)
        if length <= 0:
            continue
        midpoint = Point(
            start.x + ux * (opening.offset + opening.width / 2) / length,
            start.y + uy * (opening.offset + opening.width / 2) / length,
        )
        if polygon.boundary.distance(midpoint) <= max(180.0, opening.width * 0.35):
            allowed.append(opening.type.value)
    if not allowed:
        return "Architectural openings allowed in this room: exactly 0 (all walls must remain solid)."
    counts = ", ".join(f"{kind}×{allowed.count(kind)}" for kind in sorted(set(allowed)))
    return f"Architectural openings allowed in this room: exactly {len(allowed)} total ({counts}); no others."


def load_preview_scene_geometry(project_id: str) -> SceneGeometry:
    path = _data_root() / project_id / "scene-geometry.preview-v1.json"
    return SceneGeometry.model_validate_json(path.read_text(encoding="utf-8"))


def _write_model(path: Path, model: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # Preserve exactly the fields that participated in the canonical payload:
    # explicit nulls stay, optional defaults that were absent stay absent.
    temporary.write_text(model.model_dump_json(indent=2, exclude_unset=True), encoding="utf-8")
    temporary.replace(path)


def _data_root() -> Path:
    return Path(os.environ.get("PANO_DATA_ROOT", DEFAULT_DATA_ROOT))


def _distance(a, b) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _room_name(room_type: str) -> str:
    return {
        "bedroom": "卧室", "living": "客厅", "dining": "餐厅",
        "livingDining": "客餐厅", "kitchen": "厨房", "bathroom": "卫生间",
        "balcony": "阳台", "study": "书房", "other": "其他空间",
    }.get(room_type, room_type)
