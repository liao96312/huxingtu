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
from collections.abc import Callable
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
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """Run the vertical slice synchronously and return user-facing evidence."""

    def report(progress: int, stage: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(progress, stage)
        except Exception:
            # Observability must never turn an otherwise deliverable preview
            # into a failed task.
            pass

    report(5, "parse")
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
    report(30, "parse-complete")
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

    input_stage = next((stage for stage in run.stages if stage.stage == "input_gate"), None)
    if input_stage is not None and input_stage.status != "accepted":
        # Only a failed input gate is allowed to stop the automatic chain. A
        # weak CV/VLM stage below it is handled by the preview envelope path.
        result.update({
            "pipelineStatus": "failed",
            "message": f"输入门禁未通过：{input_stage.detail or 'invalid image'}",
        })
        report(100, "failed")
        return result

    try:
        report(45, "geometry")
        layout, rooms, scene = _publish_preview(project_id, image_bytes, run)
        report(60, "structure-preview")
        manifest, ai_generated_count, ai_failed_rooms, superres_count, superres_failed_rooms = _render_panoramas(
            project_id, layout, rooms, generate_ai=generate_ai
        )
        report(95, "panorama-complete")
    except PipelineNotReady as error:
        result["pipelineStatus"] = "needsReview"
        result["message"] = str(error)
        report(100, "needs-review")
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
    cv_opening_path = _data_root() / project_id / "automatic-cv-openings.json"
    if cv_opening_path.is_file():
        cv_opening_evidence = json.loads(cv_opening_path.read_text(encoding="utf-8"))
        result["automaticCvDoorCount"] = sum(
            item["type"] == "door" for item in cv_opening_evidence["confirmed"]
        )
        result["automaticCvWindowCount"] = sum(
            item["type"] == "window" for item in cv_opening_evidence["confirmed"]
        )
    structural_fallback_path = _data_root() / project_id / "structural-fallback-rooms.json"
    if structural_fallback_path.is_file():
        structural_fallback = json.loads(structural_fallback_path.read_text(encoding="utf-8"))
        if structural_fallback["rooms"]:
            result["structuralFallbackRooms"] = structural_fallback["rooms"]
            warnings.append("以下房间结构渲染异常，已自动使用保底全景：" + ", ".join(structural_fallback["rooms"]))
    fallback_path = _data_root() / project_id / "automatic-fallback.json"
    if fallback_path.is_file():
        fallback_evidence = json.loads(fallback_path.read_text(encoding="utf-8"))
        if fallback_evidence["reasonCodes"]:
            result["automaticFallbackCodes"] = fallback_evidence["reasonCodes"]
            warnings.append("识别证据不足，已自动降级为可浏览预览：" + ", ".join(fallback_evidence["reasonCodes"]))
    if ai_failed_rooms:
        warnings.append(f"以下房间AI失败，已回退结构全景：{', '.join(ai_failed_rooms)}")
    if superres_failed_rooms:
        warnings.append(f"以下房间本地超分失败，保留AI原图：{', '.join(superres_failed_rooms)}")
    if warnings:
        result["warning"] = "；".join(warnings)
    report(100, "done")
    return result


def _publish_preview(
    project_id: str, image_bytes: bytes, run: ParseRun
) -> tuple[LayoutRevision, DerivedRoomSet, SceneGeometry]:
    fusion = next((stage for stage in run.stages if stage.stage == "fusion"), None)
    cv_stage = next((stage for stage in run.stages if stage.stage == "cv_structure"), None)
    # The viewer path must remain automatic even when recognition is weak. Use
    # the best available geometry first, then a conservative CV/envelope
    # preview; review status is evidence, not a user-action gate.
    from .floorplan.fusion import FusedRoom
    from .floorplan.fusion_openings import arbitrate_cv_door_arcs, arbitrate_cv_windows
    from .floorplan.rooms import polygonize_rooms
    from .floorplan.wall_graph import WallGraph, WallSegment

    cv_data = cv_stage.data if cv_stage is not None else {}
    graph = (fusion.data.get("wallGraph") if fusion is not None else None) or cv_data.get("wallGraph")
    decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if decoded is not None:
        image_height, image_width = decoded.shape[:2]
    else:
        # Direct unit callers can provide an already-built graph without image
        # bytes. The public upload endpoint rejects undecodable input earlier;
        # infer a canvas here so the automatic preview contract stays intact.
        points = [point for segment in getattr(graph, "segments", ()) for point in (segment.start, segment.end)]
        image_width = max(1200, int(max((point[0] for point in points), default=0) + 50))
        image_height = max(800, int(max((point[1] for point in points), default=0) + 50))
    wall_mask = cv_data.get("wallMask")
    annotation_mask = None
    if wall_mask is None and decoded is not None:
        try:
            from .floorplan.preprocess import binarize, measure_strokes, separate_layers, to_grayscale

            gray = to_grayscale(decoded)
            binary = binarize(gray)
            layers = separate_layers(binary.ink, measure_strokes(binary.ink))
            wall_mask = layers.wall
            annotation_mask = layers.annotation
        except Exception:
            wall_mask = None
            annotation_mask = None
    source_data = fusion.data if fusion is not None else cv_data
    fallback_codes: list[str] = []

    if graph is None:
        graph = _fallback_wall_graph(image_width, image_height)
        fallback_codes.append("AUTO_ENVELOPE_FALLBACK")

    closed_rooms = tuple(fusion.data.get("fusedRooms") or ()) if fusion is not None else ()
    confirmed_openings = fusion.data.get("confirmedOpenings") or () if fusion is not None else ()
    if not closed_rooms:
        cv_rooms = polygonize_rooms(
            graph, float(getattr(graph, "wall_width_px", 0) or cv_data.get("wallWidthPx") or 0),
            (image_height, image_width), wall_mask=wall_mask,
        )
        closed_rooms = tuple(
            FusedRoom(
                room.id, "other", room.polygon, room.area_mm2,
                room.area_px2, "review", ("CV_ONLY_ROOM",),
            )
            for room in cv_rooms
        )
        confirmed_openings = ()
        if closed_rooms:
            fallback_codes.append("CV_ONLY_ROOM")

    if not closed_rooms:
        # Last-resort envelope keeps a valid image moving through modeling and
        # panorama generation. It is intentionally generic and review-marked.
        polygon = _fallback_room_polygon(graph, image_width, image_height)
        area_px2 = abs(_polygon_area_px(polygon))
        closed_rooms = (
            FusedRoom(
                "auto_room_000", "other", polygon, area_px2,
                area_px2, "review", ("AUTO_ENVELOPE_FALLBACK",),
            ),
        )
        confirmed_openings = ()
        fallback_codes.append("AUTO_ENVELOPE_FALLBACK")

    if len(graph.segments) < 3:
        graph = _fallback_wall_graph(image_width, image_height, graph=graph)
        fallback_codes.append("AUTO_WALL_ENVELOPE_FALLBACK")

    cv_opening_result = arbitrate_cv_door_arcs(
        cv_data.get("doorArcProposals") or (), graph, closed_rooms,
        image_width, image_height,
    )
    cv_window_result = arbitrate_cv_windows(graph, closed_rooms, annotation_mask)
    existing_intervals = list(confirmed_openings)
    automatic_cv_confirmed = []
    for result in (cv_window_result, cv_opening_result):
        for candidate in result.confirmed:
            if any(
                candidate.wall_edge_id == existing.wall_edge_id
                and _segments_overlap_px(
                    candidate.start_px, candidate.end_px,
                    existing.start_px, existing.end_px,
                )
                for existing in existing_intervals
            ):
                continue
            existing_intervals.append(candidate)
            automatic_cv_confirmed.append(candidate)
    confirmed_openings = tuple(existing_intervals)

    mm_per_px = float(source_data.get("mmPerPx") or cv_data.get("mmPerPx") or graph.mm_per_px or 0)
    if not math.isfinite(mm_per_px) or mm_per_px <= 0:
        mm_per_px = _estimate_preview_scale(image_width, image_height)
        fallback_codes.append("AUTO_SCALE_ESTIMATE")
    if fallback_codes:
        # Preserve diagnostics on the parse stage without promoting this to a
        # confirmed revision. The generated preview remains fully viewable.
        fallback_codes = list(dict.fromkeys(fallback_codes))

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
            "pixelToWorldTransform": transform,
            "scaleConfidence": 0.1 if fallback_codes else 0.5,
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
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "automatic-cv-openings.json").write_text(
        json.dumps({
            "schemaVersion": "0.1.0", "projectId": project_id,
            "confirmed": [
                {"id": item.id, "type": item.type, "wallEdgeId": item.wall_edge_id,
                 "startPx": item.start_px, "endPx": item.end_px,
                 "widthMm": item.width_mm, "sideRoomIds": item.side_room_ids,
                 "reasonCodes": item.reason_codes}
                for item in automatic_cv_confirmed
            ],
            "rejected": [
                {"id": item.id, "wallEdgeId": item.wall_edge_id,
                 "reasonCodes": item.reason_codes}
                for item in (*cv_window_result.rejected, *cv_opening_result.rejected)
            ],
        }, ensure_ascii=False, indent=2, default=list), encoding="utf-8",
    )
    (folder / "automatic-fallback.json").write_text(
        json.dumps({
            "schemaVersion": "0.1.0", "projectId": project_id,
            "status": "preview", "reasonCodes": fallback_codes,
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_model(folder / "layout-revision.preview-v1.json", layout)
    _write_model(folder / "rooms.preview-v1.json", rooms)
    _write_model(folder / "scene-geometry.preview-v1.json", scene)
    return layout, rooms, scene


def _estimate_preview_scale(width: int, height: int) -> float:
    """Estimate a display-only scale when neither CV nor VLM has one."""

    return max(1.0, min(10.0, 6000.0 / max(width, height, 1)))


def _fallback_room_polygon(graph, width: int, height: int) -> tuple[tuple[float, float], ...]:
    points = [point for segment in getattr(graph, "segments", ()) for point in (segment.start, segment.end)]
    if points:
        left = max(0.0, min(point[0] for point in points))
        right = min(float(width - 1), max(point[0] for point in points))
        top = max(0.0, min(point[1] for point in points))
        bottom = min(float(height - 1), max(point[1] for point in points))
    else:
        margin_x, margin_y = width * 0.05, height * 0.05
        left, right = margin_x, width - margin_x
        top, bottom = margin_y, height - margin_y
    if right - left < 40 or bottom - top < 40:
        margin_x, margin_y = width * 0.05, height * 0.05
        left, right = margin_x, width - margin_x
        top, bottom = margin_y, height - margin_y
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def _fallback_wall_graph(width: int, height: int, *, graph=None) -> WallGraph:
    from .floorplan.wall_graph import WallGraph, WallSegment

    polygon = _fallback_room_polygon(graph, width, height) if graph is not None else (
        (width * 0.05, height * 0.05),
        (width * 0.95, height * 0.05),
        (width * 0.95, height * 0.95),
        (width * 0.05, height * 0.95),
    )
    thickness = max(4.0, min(width, height) * 0.01)
    segments = tuple(
        WallSegment(
            f"auto_boundary_{index + 1}",
            "horizontal" if start[1] == end[1] else "vertical",
            start, end, thickness, False,
        )
        for index, (start, end) in enumerate(zip(polygon, polygon[1:] + polygon[:1]))
    )
    return WallGraph(
        horizontal_axes=tuple(sorted({point[1] for point in polygon})),
        vertical_axes=tuple(sorted({point[0] for point in polygon})),
        segments=segments,
        gaps=(),
        mm_per_px=0.0,
        wall_width_px=thickness,
    )


def _polygon_area_px(points: tuple[tuple[float, float], ...]) -> float:
    return abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    ) / 2.0) if len(points) >= 3 else 0.0


def _segments_overlap_px(left_start, left_end, right_start, right_end, tolerance: float = 8.0) -> bool:
    axis = 0 if abs(left_end[0] - left_start[0]) >= abs(left_end[1] - left_start[1]) else 1
    left = sorted((left_start[axis], left_end[axis]))
    right = sorted((right_start[axis], right_end[axis]))
    return min(left[1], right[1]) - max(left[0], right[0]) > tolerance


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
    structural_fallback_rooms: list[str] = []
    ordered_rooms = sorted(rooms.rooms, key=lambda room: room.areaMm2, reverse=True)
    for order, room in enumerate(ordered_rooms):
        scene_id = f"{room.id}-main"
        folder = _data_root() / project_id / "panorama" / scene_id
        folder.mkdir(parents=True, exist_ok=True)
        structural = folder / "structural-erp-2048x1024.jpg"
        try:
            image = render_structural_erp(layout, room.model_dump(mode="python"), ERP_WIDTH)
        except Exception:
            # One malformed room must not cancel the remaining room jobs.
            image = _fallback_structural_erp()
            structural_fallback_rooms.append(room.name)
        cv2.imwrite(str(structural), image, [cv2.IMWRITE_JPEG_QUALITY, 94])

        original = structural
        superres_paths: dict[str, Path] | None = None
        ai_ready = False
        if generate_ai:
            try:
                candidate = _generate_ai_panorama(
                    structural, folder / "ai-erp-2048x1024.jpg", room.type,
                    _opening_hint(layout, room),
                )
                qa = _qa_ai_panorama_structure(structural, candidate)
                (folder / "ai-structure-qa.json").write_text(
                    json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                if qa["status"] != "accepted":
                    # A photorealistic result that drifts from the vector
                    # topology is worse than a clearly labelled structural
                    # preview: it can visually invent doors or wall breaks.
                    ai_failed_rooms.append(f"{room.name}（结构校验失败）")
                else:
                    original = candidate
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
    project_folder = _data_root() / project_id
    project_folder.mkdir(parents=True, exist_ok=True)
    (project_folder / "structural-fallback-rooms.json").write_text(
        json.dumps({"schemaVersion": "0.1.0", "projectId": project_id, "rooms": structural_fallback_rooms}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_model(project_folder / "pano-manifest.json", manifest)
    return manifest, ai_generated_count, ai_failed_rooms, superres_count, superres_failed_rooms


def _fallback_structural_erp() -> np.ndarray:
    """Return a neutral 2:1 ERP when one room's vector render is malformed."""

    height, width = ERP_WIDTH // 2, ERP_WIDTH
    image = np.empty((height, width, 3), dtype=np.uint8)
    image[:] = (226, 232, 238)
    image[int(height * 0.72):] = (190, 205, 214)
    image[:int(height * 0.18)] = (238, 241, 244)
    for row in (height // 2, int(height * 0.72), int(height * 0.86)):
        image[max(0, row - 1):row + 1] = (150, 168, 180)
    return image


def _qa_ai_panorama_structure(reference: Path, candidate: Path) -> dict[str, Any]:
    """Check coarse ERP topology before promoting an AI panorama.

    This is deliberately a proxy gate, not semantic segmentation.  It catches
    blank/malformed outputs and large structural drift while allowing furniture
    texture that does not dominate the low-frequency edge map.  Horizontal
    distance wraps at the ERP seam so a seam-adjacent wall is not penalized.
    """

    reference_image = cv2.imread(str(reference), cv2.IMREAD_COLOR)
    candidate_image = cv2.imread(str(candidate), cv2.IMREAD_COLOR)
    if reference_image is None or candidate_image is None:
        raise ValueError("AI structure QA requires readable reference and candidate images")

    size = (512, 256)

    def low_frequency_edges(image: np.ndarray) -> np.ndarray:
        resized = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (0, 0), 2.2)
        # Structural ERP uses deliberately soft gradients; low adaptive
        # thresholds keep those wall boundaries visible after downsampling.
        return cv2.Canny(blurred, 10, 30) > 0

    reference_edge = low_frequency_edges(reference_image)
    candidate_edge = low_frequency_edges(candidate_image)
    # render_structural_erp intentionally includes three visual guide bands;
    # they are useful to a human but not architectural topology.
    for ratio in (0.50, 0.72, 0.86):
        row = int(reference_edge.shape[0] * ratio)
        for edge in (reference_edge, candidate_edge):
            edge[max(0, row - 2):min(edge.shape[0], row + 3)] = False

    def long_line_count(edge: np.ndarray) -> int:
        lines = cv2.HoughLinesP(
            edge.astype(np.uint8) * 255, 1, np.pi / 180,
            threshold=20, minLineLength=30, maxLineGap=5,
        )
        return 0 if lines is None else int(len(lines))

    def wrapped_distance(edge: np.ndarray) -> np.ndarray:
        tiled = np.concatenate((edge, edge, edge), axis=1)
        distance = cv2.distanceTransform((~tiled).astype(np.uint8), cv2.DIST_L2, 3)
        width = edge.shape[1]
        return distance[:, width:2 * width]

    reference_density = float(reference_edge.mean())
    candidate_density = float(candidate_edge.mean())
    if reference_density <= 0:
        raise ValueError("AI structure QA reference contains no architectural edges")
    reference_long_lines = long_line_count(reference_edge)
    candidate_long_lines = long_line_count(candidate_edge)
    distance_to_candidate = wrapped_distance(candidate_edge)
    distance_to_reference = wrapped_distance(reference_edge)
    tolerance_px = 8
    reference_recall = float(
        (distance_to_candidate[reference_edge] <= tolerance_px).mean()
    ) if reference_edge.any() else 0.0
    candidate_precision = float(
        (distance_to_reference[candidate_edge] <= tolerance_px).mean()
    ) if candidate_edge.any() else 0.0
    density_ratio = candidate_density / reference_density

    reasons: list[str] = []
    if reference_recall < 0.25:
        reasons.append("STRUCTURE_EDGE_RECALL_LOW")
    if candidate_precision < 0.08:
        reasons.append("STRUCTURE_EDGE_PRECISION_LOW")
    if not 0.35 <= density_ratio <= 8.0:
        reasons.append("STRUCTURE_EDGE_DENSITY_DRIFT")
    if candidate_long_lines > max(24, reference_long_lines * 5):
        reasons.append("STRUCTURE_LONG_LINE_DENSITY_DRIFT")
    return {
        "schemaVersion": "0.1.0",
        "method": "low-frequency-wrapped-edge-proxy-v1",
        "status": "accepted" if not reasons else "rejected",
        "tolerancePx": tolerance_px,
        "referenceWidth": int(reference_image.shape[1]),
        "referenceHeight": int(reference_image.shape[0]),
        "candidateWidth": int(candidate_image.shape[1]),
        "candidateHeight": int(candidate_image.shape[0]),
        "referenceEdgeDensity": round(reference_density, 6),
        "candidateEdgeDensity": round(candidate_density, 6),
        "edgeDensityRatio": round(density_ratio, 4),
        "referenceRecallWithinTolerance": round(reference_recall, 4),
        "candidatePrecisionWithinTolerance": round(candidate_precision, 4),
        "referenceLongLineCount": reference_long_lines,
        "candidateLongLineCount": candidate_long_lines,
        "reasonCodes": reasons or ["STRUCTURE_PROXY_WITHIN_GATE"],
        "semanticIoU": None,
        "semanticF1": None,
    }


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
