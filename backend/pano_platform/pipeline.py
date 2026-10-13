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
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any

import cv2
import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polylabel

from .contracts import (
    DerivedRoomSet,
    LayoutRevision,
    PanoManifest,
    SceneGeometry,
    StyleCatalog,
    StyleContext,
    canonical_sha256,
)
from .floorplan.parse import ParseRun, ParseStatus, VisionMode, orchestrate
from .scene_geometry import build_scene_geometry

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = ROOT / "datasets" / "derived"
ERP_WIDTH = 2048
WALL_HEIGHT_MM = 2800.0
RENDER_DOOR_WIDTH_MM = 900.0
RENDER_DOOR_HEIGHT_MM = 2100.0
MIN_RENDER_WINDOW_WIDTH_MM = 900.0
STYLE_CATALOG_PATH = ROOT / "datasets" / "style-catalog.v1.json"


class PipelineNotReady(ValueError):
    pass


@lru_cache(maxsize=1)
def load_style_catalog() -> StyleCatalog:
    return StyleCatalog.model_validate_json(STYLE_CATALOG_PATH.read_text(encoding="utf-8"))


def load_style_context(style_id: str) -> StyleContext:
    style = next((item for item in load_style_catalog().styles if item.id == style_id), None)
    if style is None:
        raise ValueError(f"unknown styleId: {style_id}")
    return style


def run_upload_pipeline(
    image_bytes: bytes,
    *,
    project_id: str,
    allow_external_vision: bool,
    generate_ai: bool = True,
    style_id: str = "modern",
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

    style = load_style_context(style_id)
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
    ai_blocked_by_geometry = generate_ai and run.status is not ParseStatus.ACCEPTED
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
            project_id, layout, rooms,
            generate_ai=generate_ai and not ai_blocked_by_geometry,
            style_id=style.id,
            progress_callback=report,
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
        "styleId": style.id,
        "styleLabel": style.label,
        "aiGenerated": ai_generated_count > 0,
        "aiGeneratedCount": ai_generated_count,
        "aiFailedRooms": ai_failed_rooms,
        "superresGeneratedCount": superres_count,
        "superresFailedRooms": superres_failed_rooms,
    })
    warnings = []
    if ai_blocked_by_geometry:
        result["aiSkippedReason"] = "GEOMETRY_NOT_CONFIRMED"
        warnings.append("户型几何或比例尺未确认，已跳过付费AI并发布结构预览")
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
    from .floorplan.fusion_openings import (
        arbitrate_cv_door_arcs, arbitrate_cv_windows, recover_cv_door_carriers,
    )
    from .floorplan.rooms import (
        polygonize_rooms, unsupported_boundary_room_ids, unsupported_wall_room_ids,
    )
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
            # Connected window panes can be absorbed into the reconstructed
            # wall layer. The already-localized gap crop safely uses raw ink.
            annotation_mask = binary.ink
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

    door_graph = recover_cv_door_carriers(
        cv_data.get("doorArcProposals") or (), graph, wall_mask
    )
    cv_opening_result = arbitrate_cv_door_arcs(
        cv_data.get("doorArcProposals") or (), door_graph, closed_rooms,
        image_width, image_height, wall_mask=wall_mask,
    )
    cv_window_result = arbitrate_cv_windows(
        graph, closed_rooms, annotation_mask, cv_data.get("doorArcProposals") or ()
    )
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

    # Exterior notches closed only by synthetic bbox edges are not rooms.
    # Clip after opening arbitration so entrance doors keep their topology.
    from .floorplan.footprint import clip_rooms_to_footprint

    closed_rooms, footprint_dropped, footprint_clipped = clip_rooms_to_footprint(
        closed_rooms, graph, wall_mask
    )
    if footprint_dropped:
        fallback_codes.append("CV_FOOTPRINT_EXTERIOR_ROOM_FILTERED")
    if footprint_clipped:
        fallback_codes.append("CV_FOOTPRINT_ROOM_CLIPPED")

    # Unsealed door gaps flood neighbouring rooms together; split them along
    # confirmed doors plus interior wall ink.
    from .floorplan.room_split import split_rooms_by_doors

    closed_rooms, door_split = split_rooms_by_doors(
        closed_rooms, graph, wall_mask, confirmed_openings
    )
    if door_split:
        fallback_codes.append("CV_DOOR_SPLIT_ROOM")

    unsupported_boundary_rooms = unsupported_boundary_room_ids(
        graph, closed_rooms, wall_mask
    )
    unsupported_wall_rooms = unsupported_wall_room_ids(
        closed_rooms, wall_mask, graph.wall_width_px
    )
    implausibly_small_rooms = tuple(
        room.id for room in closed_rooms if room.area_mm2 < 100_000.0
    )
    rejected_room_ids = set((
        *unsupported_boundary_rooms, *unsupported_wall_rooms, *implausibly_small_rooms,
    ))
    if rejected_room_ids and len(rejected_room_ids) < len(closed_rooms):
        closed_rooms = tuple(room for room in closed_rooms if room.id not in rejected_room_ids)
        if unsupported_boundary_rooms:
            fallback_codes.append("CV_UNSUPPORTED_BOUNDARY_ROOM_FILTERED")
        if unsupported_wall_rooms:
            fallback_codes.append("CV_UNSUPPORTED_WALL_ROOM_FILTERED")
        if implausibly_small_rooms:
            fallback_codes.append("CV_IMPLAUSIBLY_SMALL_ROOM_FILTERED")

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

    confirmed_edge_ids = {opening.wall_edge_id for opening in confirmed_openings}
    door_carriers = tuple(
        segment for segment in door_graph.segments[len(graph.segments):]
        if segment.id in confirmed_edge_ids
    )
    layout_segments = _extend_opening_carriers(
        (*graph.segments, *door_carriers), confirmed_openings
    )
    edges = [
        {
            "id": segment.id,
            "startNodeId": node_id(segment.start),
            "endNodeId": node_id(segment.end),
            "thickness": min(300.0, max(80.0, segment.thickness_px * mm_per_px)),
            "height": WALL_HEIGHT_MM,
            "confidence": 0.5,
        }
        for segment in layout_segments
        if segment.start != segment.end
    ]
    if len(nodes) < 3 or len(edges) < 3:
        raise PipelineNotReady("有效墙段少于3条，不能构建3D场景")

    segments = {segment.id: segment for segment in layout_segments}
    openings = []
    for item in confirmed_openings:
        segment = segments.get(item.wall_edge_id)
        if segment is None or item.width_mm is None:
            continue
        segment_start = tuple(round(value, 3) for value in segment.start)
        segment_end = tuple(round(value, 3) for value in segment.end)
        start_offset = _distance(segment_start, item.start_px) * mm_per_px
        end_offset = _distance(segment_start, item.end_px) * mm_per_px
        offset, width = min(start_offset, end_offset), abs(end_offset - start_offset)
        edge_length = _distance(segment_start, segment_end) * mm_per_px
        width = min(width, max(0.0, edge_length - offset))
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
    (folder / "automatic-room-evidence.json").write_text(
        json.dumps({
            "schemaVersion": "0.1.0", "projectId": project_id,
            "unsupportedBoundaryRoomIds": list(unsupported_boundary_rooms),
            "unsupportedWallRoomIds": list(unsupported_wall_rooms),
            "implausiblySmallRoomIds": list(implausibly_small_rooms),
        }, ensure_ascii=False, indent=2), encoding="utf-8",
    )
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
                {"id": item.id, "type": item.type, "wallEdgeId": item.wall_edge_id,
                 "startPx": item.start_px, "endPx": item.end_px,
                 "widthMm": item.width_mm, "sideRoomIds": item.side_room_ids,
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


def _extend_opening_carriers(segments, openings):
    """Extend a confirmed opening's chosen collinear edge to hold its full span."""

    by_id = {segment.id: segment for segment in segments}
    for opening in openings:
        if opening.wall_edge_id not in by_id:
            continue
        segment = by_id[opening.wall_edge_id]
        if segment.axis == "horizontal":
            low = min(segment.start[0], segment.end[0], opening.start_px[0], opening.end_px[0])
            high = max(segment.start[0], segment.end[0], opening.start_px[0], opening.end_px[0])
            by_id[segment.id] = replace(segment, start=(low, segment.start[1]), end=(high, segment.start[1]))
        else:
            low = min(segment.start[1], segment.end[1], opening.start_px[1], opening.end_px[1])
            high = max(segment.start[1], segment.end[1], opening.start_px[1], opening.end_px[1])
            by_id[segment.id] = replace(segment, start=(segment.start[0], low), end=(segment.start[0], high))
    return tuple(by_id[segment.id] for segment in segments)


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


def _initial_view_yaw(structural_erp: np.ndarray) -> float:
    """Start on a rendered door instead of an arbitrary blank wall."""

    door = np.max(
        np.abs(structural_erp.astype(np.int16) - np.array((142, 174, 198))), axis=2,
    ) <= 12
    columns = np.flatnonzero(door.any(axis=0))
    if columns.size:
        # Keep the door visible without making it fill the exact center of the
        # first frame; the small offset reveals surrounding wall context.
        yaw = float(np.median(columns) / structural_erp.shape[1] * 360.0 + 15.0)
    else:
        floor = np.max(
            np.abs(structural_erp.astype(np.int16) - np.array((190, 205, 214))), axis=2,
        ) <= 12
        floor_start = np.where(floor.any(axis=0), floor.argmax(axis=0), structural_erp.shape[0])
        # Corners communicate room depth; the strongest circular change in the
        # wall/floor boundary is a better first view than the middle of a blank wall.
        span = max(3, structural_erp.shape[1] // 90)
        corner_strength = np.abs(
            np.roll(floor_start, -span).astype(np.int32)
            - np.roll(floor_start, span).astype(np.int32)
        )
        yaw = float(np.argmax(corner_strength) / structural_erp.shape[1] * 360.0)
    return yaw - 360.0 if yaw > 180.0 else yaw


def _render_scale_factor(layout: LayoutRevision, rooms) -> float:
    """Normalize obviously unitless geometry for a visually plausible preview."""

    candidates = []
    door_widths = sorted(opening.width for opening in layout.openings if opening.type.value == "door")
    if door_widths:
        typical_door = median(door_widths)
        if typical_door < 650 or typical_door > 1200:
            candidates.append(RENDER_DOOR_WIDTH_MM / typical_door)
    room_areas = sorted(room.areaMm2 for room in rooms if room.areaMm2 > 0)
    if room_areas:
        typical_area = median(room_areas)
        if typical_area < 3_000_000 or typical_area > 35_000_000:
            candidates.append(math.sqrt(8_000_000 / typical_area))
    # ponytail: semantic priors only normalize previews; real measurements
    # still require explicit scale evidence.
    return min(4.0, max(0.25, median(candidates))) if candidates else 1.0


def _render_panoramas(
    project_id: str,
    layout: LayoutRevision,
    rooms: DerivedRoomSet,
    *,
    generate_ai: bool,
    style_id: str = "modern",
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[PanoManifest, int, list[str], int, list[str]]:
    style = load_style_context(style_id)
    scenes = []
    ai_generated_count = 0
    ai_failed_rooms: list[str] = []
    superres_count = 0
    superres_failed_rooms: list[str] = []
    structural_fallback_rooms: list[str] = []
    enclosing_rooms = _enclosing_room_map(rooms.rooms)
    ordered_rooms = sorted(
        rooms.rooms, key=lambda room: (room.id in enclosing_rooms, -room.areaMm2)
    )
    render_scale = _render_scale_factor(layout, rooms.rooms)
    for order, room in enumerate(ordered_rooms):
        scene_id = f"{room.id}-main"
        folder = _data_root() / project_id / "panorama" / scene_id
        folder.mkdir(parents=True, exist_ok=True)
        structural = folder / "structural-erp-2048x1024.jpg"
        try:
            image = render_structural_erp(
                layout, room.model_dump(mode="python"), ERP_WIDTH,
                physical_scale=render_scale,
            )
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
                opening_hint = _opening_hint(layout, room)
                expected_openings = _expected_room_opening_counts(layout, room)
                for attempt in range(2):
                    candidate = _generate_ai_panorama(
                        structural,
                        folder / ("ai-erp-source.jpg" if attempt == 0 else "ai-erp-source-retry.jpg"),
                        room.type, opening_hint, style,
                    )
                    qa = _qa_ai_panorama_structure(structural, candidate, room.type)
                    precision_only = qa["reasonCodes"] == ["STRUCTURE_EDGE_PRECISION_LOW"]
                    if qa["status"] == "accepted" or precision_only:
                        semantic_qa = _qa_ai_opening_semantics(candidate, expected_openings)
                        qa["openingSemantics"] = semantic_qa
                        if semantic_qa["status"] == "accepted" and precision_only:
                            qa["status"] = "accepted"
                            qa["reasonCodes"] = ["STRUCTURE_EDGE_PRECISION_OVERRIDDEN_BY_SEMANTIC_QA"]
                        elif semantic_qa["status"] != "accepted":
                            qa["status"] = "rejected"
                            qa["reasonCodes"] = list(dict.fromkeys((
                                *qa["reasonCodes"], *semantic_qa["reasonCodes"],
                            )))
                        if semantic_qa["status"] == "rejected" and attempt == 0:
                            opening_hint += _opening_semantic_retry_instruction(semantic_qa)
                            continue
                    break
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
        proxy_2k = folder / "proxy-2048x1024.jpg"
        thumb = folder / "thumb-320x160.jpg"
        loaded = cv2.imread(str(original), cv2.IMREAD_COLOR)
        original_height, original_width = loaded.shape[:2]
        asset_4k = superres_paths["proxy4k"] if superres_paths else None
        asset_2k = superres_paths["proxy2k"] if superres_paths else proxy_2k
        asset_1k = superres_paths["proxy1k"] if superres_paths else proxy
        if not superres_paths:
            cv2.imwrite(str(proxy_2k), cv2.resize(loaded, (2048, 1024), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 92])
            cv2.imwrite(str(proxy), cv2.resize(loaded, (1024, 512), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 90])
        cv2.imwrite(str(thumb), cv2.resize(loaded, (320, 160), interpolation=cv2.INTER_AREA), [cv2.IMWRITE_JPEG_QUALITY, 85])
        base = f"/artifacts/{project_id}/panorama/{scene_id}"
        original_name = original.name
        scenes.append({
            "id": scene_id, "roomId": room.id, "roomName": room.name,
            "title": f"{room.name}{' AI+超分预览' if superres_paths else ' AI预览' if ai_ready else ' 结构预览'}",
            "order": order,
            "assets": {
                "original": {"url": f"{base}/{original_name}", "width": original_width, "height": original_height, "contentType": "image/jpeg"},
                "proxy4k": {"url": f"{base}/{asset_4k.name}", "width": 4096, "height": 2048, "contentType": "image/jpeg"} if asset_4k else None,
                "proxy2k": {"url": f"{base}/{asset_2k.name}", "width": 2048, "height": 1024, "contentType": "image/jpeg"},
                "proxy1k": {"url": f"{base}/{asset_1k.name}", "width": 1024, "height": 512, "contentType": "image/jpeg"},
                "thumbnailUrl": f"{base}/{thumb.name}",
            },
            # Three.js interprets this as vertical FOV; 82° becomes an
            # extreme ~119° horizontal view on a 16:9 screen.
            "initialView": {"yaw": _initial_view_yaw(image), "pitch": 0.0, "fov": 55.0},
        })
        if progress_callback is not None:
            progress_callback(
                60 + round(34 * (order + 1) / len(ordered_rooms)),
                f"panorama-room-{order + 1}-of-{len(ordered_rooms)}",
            )

    scene_by_room = {scene["roomId"]: scene["id"] for scene in scenes}
    hotspots_by_room = _build_room_hotspots(layout, rooms, scene_by_room)
    for scene in scenes:
        scene["hotspots"] = hotspots_by_room.get(scene["roomId"], [])

    payload = {
        "schemaVersion": "0.1.0", "projectId": project_id, "revision": 1,
        "defaultSceneId": scenes[0]["id"], "projection": "equirectangular",
        "scenes": scenes, "share": {"title": f"{project_id} · {style.label}全景预览"},
    }
    payload["contentHash"] = canonical_sha256(payload)
    manifest = PanoManifest.model_validate(payload)
    project_folder = _data_root() / project_id
    project_folder.mkdir(parents=True, exist_ok=True)
    room_evidence_path = project_folder / "automatic-room-evidence.json"
    room_evidence = (
        json.loads(room_evidence_path.read_text(encoding="utf-8"))
        if room_evidence_path.is_file() else {}
    )
    room_evidence.update({
            "schemaVersion": "0.1.0", "projectId": project_id,
            "renderScaleFactor": round(render_scale, 4),
            "suspectedEnclosingRooms": [
                {"roomId": room_id, "containedRoomIds": list(contained)}
                for room_id, contained in sorted(enclosing_rooms.items())
            ],
        })
    room_evidence_path.write_text(
        json.dumps(room_evidence, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (project_folder / "structural-fallback-rooms.json").write_text(
        json.dumps({"schemaVersion": "0.1.0", "projectId": project_id, "rooms": structural_fallback_rooms}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_model(project_folder / "pano-manifest.json", manifest)
    return manifest, ai_generated_count, ai_failed_rooms, superres_count, superres_failed_rooms


def _enclosing_room_map(rooms, containment_ratio: float = 0.9) -> dict[str, tuple[str, ...]]:
    """Find exterior-shell candidates without deleting topology evidence."""

    polygons = {
        room.id: Polygon([(point.x, point.y) for point in room.polygon])
        for room in rooms
    }
    result = {}
    for room in rooms:
        candidate = polygons[room.id]
        contained = tuple(
            other_id
            for other_id, other in polygons.items()
            if other_id != room.id
            and other.area > 0
            and candidate.intersection(other).area / other.area >= containment_ratio
        )
        if len(contained) >= 2:
            result[room.id] = contained
    return result


def _build_room_hotspots(layout: LayoutRevision, rooms: DerivedRoomSet, scene_by_room: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Build conservative bidirectional room links from confirmed door geometry."""

    nodes = {node.id: node for node in layout.nodes}
    room_polygons = {
        room.id: Polygon([(point.x, point.y) for point in room.polygon])
        for room in rooms.rooms
    }
    room_names = {room.id: room.name for room in rooms.rooms}
    links: dict[tuple[str, str], dict[str, Any]] = {}
    for opening in layout.openings:
        if opening.type.value != "door":
            continue
        edge = next((item for item in layout.edges if item.id == opening.wallEdgeId), None)
        if edge is None:
            continue
        start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
        length = math.hypot(end.x - start.x, end.y - start.y)
        if length <= 0:
            continue
        ratio = (opening.offset + opening.width / 2) / length
        midpoint = Point(start.x + (end.x - start.x) * ratio, start.y + (end.y - start.y) * ratio)
        nearby = sorted(
            (polygon.boundary.distance(midpoint), room_id)
            for room_id, polygon in room_polygons.items()
            if polygon.boundary.distance(midpoint) <= max(300.0, edge.thickness * 3)
        )
        if len(nearby) < 2:
            continue
        first, second = nearby[0][1], nearby[1][1]
        if first == second or first not in scene_by_room or second not in scene_by_room:
            continue
        for source, target in ((first, second), (second, first)):
            source_point = room_polygons[source].representative_point()
            yaw = math.degrees(math.atan2(midpoint.y - source_point.y, midpoint.x - source_point.x))
            links.setdefault((source, target), {
                "id": f"hotspot-{opening.id}-{source}-to-{target}",
                "label": f"去{room_names[target]}",
                "targetSceneId": scene_by_room[target],
                "yaw": max(-180.0, min(180.0, yaw)),
                "pitch": 0.0,
            })
    result: dict[str, list[dict[str, Any]]] = {}
    for (source, _), hotspot in sorted(links.items()):
        result.setdefault(source, []).append(hotspot)
    return result


def _fallback_structural_erp() -> np.ndarray:
    """Return a neutral 2:1 ERP when one room's vector render is malformed."""

    height, width = ERP_WIDTH // 2, ERP_WIDTH
    image = np.empty((height, width, 3), dtype=np.uint8)
    image[:] = (226, 232, 238)
    image[int(height * 0.72):] = (190, 205, 214)
    image[:int(height * 0.18)] = (238, 241, 244)
    return image


def _qa_ai_panorama_structure(reference: Path, candidate: Path, room_type: str | None = None) -> dict[str, Any]:
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
    seam_mae = _erp_seam_mae(candidate_image)
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

    cleanliness = _wall_cleanliness_proxy(reference_image, candidate_image, room_type)
    reasons = _structure_gate_reasons(
        reference_recall, candidate_precision, density_ratio,
        reference_long_lines, candidate_long_lines, cleanliness["status"], seam_mae,
    )
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
        "seamMae": round(seam_mae, 3),
        "wallCleanliness": cleanliness,
        "reasonCodes": reasons or ["STRUCTURE_PROXY_WITHIN_GATE"],
        "semanticIoU": None,
        "semanticF1": None,
    }


def _structure_gate_reasons(
    reference_recall: float,
    candidate_precision: float,
    density_ratio: float,
    reference_long_lines: int,
    candidate_long_lines: int,
    cleanliness_status: str,
    seam_mae: float = 0.0,
) -> list[str]:
    """Fail closed on customer-visible structural drift."""

    reasons = []
    if reference_recall < 0.70:
        reasons.append("STRUCTURE_EDGE_RECALL_LOW")
    if candidate_precision < 0.30:
        reasons.append("STRUCTURE_EDGE_PRECISION_LOW")
    if not 0.35 <= density_ratio <= 8.0:
        reasons.append("STRUCTURE_EDGE_DENSITY_DRIFT")
    if candidate_long_lines > max(40, reference_long_lines * 8):
        reasons.append("STRUCTURE_LONG_LINE_DENSITY_DRIFT")
    if cleanliness_status == "rejected":
        reasons.append("WALL_SURFACE_UNEVEN")
    if seam_mae > 8.0:
        reasons.append("ERP_SEAM_DISCONTINUITY")
    return reasons


def _erp_seam_mae(image: np.ndarray) -> float:
    """Compare equal-distance pixels on both sides of the wrapped ERP seam."""

    strip = max(2, image.shape[1] // 256)
    left = image[:, :strip].astype(np.float32)
    right = image[:, -strip:][:, ::-1].astype(np.float32)
    return float(np.abs(left - right).mean())


def _erp_review_contact_sheet(image: np.ndarray, width: int = 512, height: int = 320) -> np.ndarray:
    """Project an ERP into four non-overlapping 90-degree views for visual QA."""

    source_height, source_width = image.shape[:2]
    screen_x, screen_y = np.meshgrid(np.arange(width), np.arange(height))
    right = (screen_x - (width - 1) / 2) / ((width - 1) / 2)
    up = -((screen_y - (height - 1) / 2) / ((height - 1) / 2)) * height / width
    views = []
    for yaw in (0.0, np.pi / 2, np.pi, 3 * np.pi / 2):
        direction_x = np.cos(yaw) - right * np.sin(yaw)
        direction_y = np.sin(yaw) + right * np.cos(yaw)
        longitude = np.mod(np.arctan2(direction_y, direction_x), 2 * np.pi)
        latitude = np.arctan2(up, np.hypot(direction_x, direction_y))
        map_x = (longitude / (2 * np.pi) * source_width).astype(np.float32)
        map_y = ((np.pi / 2 - latitude) / np.pi * source_height).astype(np.float32)
        views.append(cv2.remap(image, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP))
    return np.vstack((np.hstack(views[:2]), np.hstack(views[2:])))


def _wall_cleanliness_proxy(reference: np.ndarray, candidate: np.ndarray, room_type: str | None) -> dict[str, Any]:
    """Reject obvious low-frequency blotches only where painted walls dominate."""

    eligible = room_type in {"living", "livingDining", "bedroom", "study", "other"}
    if not eligible:
        return {"status": "skipped", "reason": "ROOM_TYPE_NOT_ELIGIBLE"}
    reference = cv2.resize(reference, (512, 256), interpolation=cv2.INTER_AREA)
    candidate = cv2.resize(candidate, (512, 256), interpolation=cv2.INTER_AREA)
    rows = np.arange(256)[:, None]
    reference_chroma = reference.max(axis=2) - reference.min(axis=2)
    candidate_chroma = candidate.max(axis=2) - candidate.min(axis=2)
    brightness = candidate.mean(axis=2)
    mask = (
        (rows >= 64) & (rows < 174)
        & (reference_chroma < 22) & (candidate_chroma < 60)
        & (brightness > 85) & (brightness < 245)
    )
    mask = cv2.erode(mask.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    coverage = float(mask.mean())
    if coverage < 0.1:
        return {"status": "skipped", "reason": "INSUFFICIENT_PAINTED_WALL_COVERAGE", "coverage": round(coverage, 4)}
    gray = cv2.cvtColor(candidate, cv2.COLOR_BGR2GRAY).astype(np.float32)
    unevenness = np.abs(
        cv2.GaussianBlur(gray, (0, 0), 3) - cv2.GaussianBlur(gray, (0, 0), 18)
    )
    p90 = float(np.percentile(unevenness[mask], 90))
    return {
        "status": "rejected" if p90 > 30 else "accepted",
        "coverage": round(coverage, 4),
        "lowFrequencyUnevennessP90": round(p90, 2),
        "threshold": 30.0,
    }


def _upscale_with_realesrgan(source: Path, folder: Path) -> dict[str, Path]:
    """Run the existing wrap-aware Real-ESRGAN tile script for one ERP."""

    tool = _resolve_realesrgan_tool()
    base = "ai-erp"
    command = [
        sys.executable, str(ROOT / "scripts" / "upscale_ai_panorama.py"),
        "--source", str(source), "--output-dir", str(folder),
        "--output-base", base, "--tool", str(tool),
        "--preserve-source", "--output-size", "8192x4096",
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
        "original": folder / f"{base}-8192x4096.jpg",
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


def _render_opening_width(opening) -> float:
    if opening.type.value == "window":
        return max(opening.width, MIN_RENDER_WINDOW_WIDTH_MM)
    return RENDER_DOOR_WIDTH_MM


def _render_opening_height(opening) -> float:
    return opening.height if opening.type.value == "window" else RENDER_DOOR_HEIGHT_MM


def _opening_render_instruction(kind: str, width: int, height: int) -> str:
    if kind == "window":
        return f"window {width}mm wide with frame and glazing"
    return (
        f"door opening exactly {width}mm wide and {height}mm high with one closed 820-850mm hinged leaf; "
        "use the same frame thickness and head height as every other door"
    )


def _choose_erp_seam_yaw(
    layout: LayoutRevision, nodes: dict, cx: float, cy: float,
    physical_scale: float = 1.0,
) -> float:
    """Place the ERP seam as far as possible from every known opening."""

    candidates = np.linspace(0.0, 2 * np.pi, 720, endpoint=False)
    clearance = np.full(candidates.shape, np.inf)
    for opening in layout.openings:
        edge = next((item for item in layout.edges if item.id == opening.wallEdgeId), None)
        if edge is None:
            continue
        start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
        length = math.hypot(end.x - start.x, end.y - start.y)
        if length <= 0:
            continue
        ratio = (opening.offset + opening.width / 2) / length
        ox = start.x + (end.x - start.x) * ratio
        oy = start.y + (end.y - start.y) * ratio
        bearing = math.atan2(oy - cy, ox - cx) % (2 * np.pi)
        distance = max(1.0, math.hypot(ox - cx, oy - cy))
        half_width = math.atan2(_render_opening_width(opening) / physical_scale / 2, distance)
        angular_distance = np.abs(np.angle(np.exp(1j * (candidates - bearing))))
        clearance = np.minimum(clearance, angular_distance - half_width)
    return float(candidates[int(np.argmax(clearance))]) if np.isfinite(clearance).any() else 0.0


def _choose_render_camera(
    layout: LayoutRevision, polygon: Polygon, nodes: dict,
    physical_scale: float,
):
    """Keep good wall clearance while avoiding edge-on door views."""

    fallback = polylabel(polygon, tolerance=10)
    openings = []
    edges = {edge.id: edge for edge in layout.edges}
    for opening in layout.openings:
        edge = edges.get(opening.wallEdgeId)
        if edge is None:
            continue
        start, end = nodes[edge.startNodeId], nodes[edge.endNodeId]
        dx, dy = end.x - start.x, end.y - start.y
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        ux, uy = dx / length, dy / length
        distance = opening.offset + opening.width / 2
        ox, oy = start.x + ux * distance, start.y + uy * distance
        if polygon.boundary.distance(Point(ox, oy)) <= max(180.0, opening.width * 0.35):
            openings.append((ox, oy, ux, uy))
    if not openings:
        return fallback

    min_x, min_y, max_x, max_y = polygon.bounds
    best = None
    for y in np.linspace(min_y, max_y, 31):
        for x in np.linspace(min_x, max_x, 31):
            point = Point(float(x), float(y))
            if not polygon.contains(point):
                continue
            clearance = polygon.boundary.distance(point)
            views = [
                (
                    abs((ox - x) * -uy + (oy - y) * ux) / max(1.0, math.hypot(ox - x, oy - y)),
                    math.hypot(ox - x, oy - y),
                )
                for ox, oy, ux, uy in openings
                if polygon.buffer(1e-6).covers(LineString(((x, y), (ox, oy))))
            ]
            viable = [view for view in views if view[0] >= 0.75]
            if clearance * physical_scale < 600 or not viable:
                continue
            # Favor a far, still-frontal door view. Multiplying by clearance
            # pulled cameras toward tiny-room centers and made doors fill the frame.
            score = max(
                frontality * min(5000.0, distance * physical_scale)
                for frontality, distance in viable
            ) + clearance * physical_scale * 0.25
            if best is None or score > best[0]:
                best = (score, point)
    return best[1] if best else fallback


def render_structural_erp(
    layout: LayoutRevision, room: dict, width: int = ERP_WIDTH,
    *, physical_scale: float = 1.0,
) -> np.ndarray:
    """Render a deterministic 2:1 ERP from vector walls with door/window holes."""

    height = width // 2
    polygon = Polygon([(point["x"], point["y"]) for point in room["polygon"]])
    nodes = {node.id: node for node in layout.nodes}
    camera = _choose_render_camera(layout, polygon, nodes, physical_scale)
    cx, cy, cz = float(camera.x), float(camera.y), 1500.0 / physical_scale
    boundary = list(polygon.exterior.coords)
    edges = [(*boundary[index], *boundary[index + 1]) for index in range(len(boundary) - 1)]
    seam_yaw = _choose_erp_seam_yaw(layout, nodes, cx, cy, physical_scale)
    angles = (np.arange(width, dtype=np.float64) / width) * (2 * np.pi) + seam_yaw
    directions = np.stack((np.cos(angles), np.sin(angles)), axis=1)
    distances = np.full((width, len(edges)), np.inf, dtype=np.float64)

    for index, (ax, ay, bx, by) in enumerate(edges):
        hit_t, hit_u = _ray_segment_intersections(cx, cy, directions, ax, ay, bx, by)
        valid = (hit_t > 1e-3) & (hit_u >= 0) & (hit_u <= 1)
        distances[valid, index] = hit_t[valid]

    order = np.argsort(distances, axis=1)
    pitch = np.pi / 2 - ((np.arange(height, dtype=np.float64) + 0.5) / height) * np.pi
    slope = np.tan(pitch)[:, None]
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = (226, 232, 238)
    unset = np.ones((height, width), dtype=bool)
    openings_by_edge: dict[str, list] = {}
    for opening in layout.openings:
        openings_by_edge.setdefault(opening.wallEdgeId, []).append(opening)
    render_openings_by_edge: dict[int, list[tuple[str, float, float, float, float, float, float, float]]] = {}
    layout_apertures = []
    for parent_edge in layout.edges:
        parent_start, parent_end = nodes[parent_edge.startNodeId], nodes[parent_edge.endNodeId]
        parent_dx, parent_dy = parent_end.x - parent_start.x, parent_end.y - parent_start.y
        parent_length = math.hypot(parent_dx, parent_dy)
        if parent_length <= 0:
            continue
        parent_ux, parent_uy = parent_dx / parent_length, parent_dy / parent_length
        for opening in openings_by_edge.get(parent_edge.id, []):
            center_distance = opening.offset + opening.width / 2
            layout_apertures.append((
                opening.type.value,
                parent_start.x + parent_ux * center_distance,
                parent_start.y + parent_uy * center_distance,
                parent_ux, parent_uy,
                _render_opening_width(opening) / physical_scale,
                (opening.sill or 0.0) / physical_scale,
                _render_opening_height(opening) / physical_scale,
                parent_edge.thickness,
            ))
    for edge_index, (start_x, start_y, end_x, end_y) in enumerate(edges):
        edge_dx, edge_dy = end_x - start_x, end_y - start_y
        edge_length = math.hypot(edge_dx, edge_dy)
        if edge_length <= 0:
            continue
        edge_ux, edge_uy = edge_dx / edge_length, edge_dy / edge_length
        apertures = []
        for kind, center_x, center_y, parent_ux, parent_uy, render_width, sill, opening_height, thickness in layout_apertures:
            if abs(edge_ux * parent_uy - edge_uy * parent_ux) > 0.01:
                continue
            line_distance = abs(
                (start_x - center_x) * parent_uy
                - (start_y - center_y) * parent_ux
            )
            if line_distance > max(250 / physical_scale, thickness * 2):
                continue
            along = (center_x - start_x) * edge_ux + (center_y - start_y) * edge_uy
            if -render_width / 2 <= along <= edge_length + render_width / 2:
                apertures.append((kind, center_x, center_y, parent_ux, parent_uy, render_width, sill, opening_height))
        render_openings_by_edge[edge_index] = apertures

    columns = np.arange(width)
    for rank in range(1):
        edge_indices = order[:, rank]
        distance = distances[columns, edge_indices]
        finite = np.isfinite(distance)
        if not finite.any():
            continue
        z = cz + slope * distance[None, :]
        solid = finite[None, :] & (z >= 0) & (z <= WALL_HEIGHT_MM / physical_scale)
        for edge_index, _edge in enumerate(edges):
            selected = edge_indices == edge_index
            if not selected.any():
                continue
            safe_distance = np.where(finite, distance, 0.0)
            hit_x = cx + directions[:, 0] * safe_distance
            hit_y = cy + directions[:, 1] * safe_distance
            for kind, center_x, center_y, opening_ux, opening_uy, render_width, sill, opening_height in render_openings_by_edge.get(edge_index, []):
                longitudinal = (hit_x - center_x) * opening_ux + (hit_y - center_y) * opening_uy
                inside_width = selected & (np.abs(longitudinal) <= render_width / 2)
                hole = inside_width[None, :] & (z >= sill) & (z <= sill + opening_height)
                solid &= ~hole
                opening_paint = unset & hole
                if opening_paint.any():
                    image[opening_paint] = (142, 174, 198) if kind == "door" else (205, 190, 160)
                    side_frame = 65 / physical_scale
                    horizontal_frame = 70 / physical_scale
                    frame = opening_paint & (
                        (np.abs(longitudinal)[None, :] >= render_width / 2 - side_frame)
                        | (z >= sill + opening_height - horizontal_frame)
                        | ((kind == "window") & (z <= sill + horizontal_frame))
                    )
                    image[frame] = (92, 112, 126) if kind == "door" else (105, 105, 105)
                    unset &= ~opening_paint
        for edge_index, (start_x, start_y, end_x, end_y) in enumerate(edges):
            paint = unset & solid & (edge_indices == edge_index)[None, :]
            if paint.any():
                length = max(1.0, math.hypot(end_x - start_x, end_y - start_y))
                brightness = int(round(226 + 10 * abs(end_x - start_x) / length))
                image[paint] = (brightness, brightness + 2, min(255, brightness + 4))
                unset &= ~paint

    floor = unset & (pitch[:, None] < 0)
    ceiling = unset & ~floor
    image[floor] = (190, 205, 214)
    image[ceiling] = (238, 241, 244)
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
    source: Path, output: Path, room_type: str, opening_hint: str = "",
    style_context: StyleContext | None = None,
) -> Path:
    import base64
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    key = os.environ.get("TUZI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TUZI_API_KEY missing")
    style = style_context or load_style_context("modern")
    model, quality, size = _tuzi_image_settings()
    prompt = (
        "Transform this exact structural 2:1 equirectangular 360 panorama into a photorealistic "
        f"{style.designStyle} {room_type} interior. Preserve every wall, architectural opening, camera position, "
        "projection and the left/right seam. This is a topology-locked edit, not a free redesign. "
        f"{opening_hint} Do not add or remove any architectural opening. Every listed door must be a complete "
        "residential door assembly with a properly sized frame, casing, door leaf and visible door handle. Keep all "
        "door leaves closed within their frames; no open leaf may project into the room or toward the camera. "
        "Every ordinary door opening must be 900mm wide and exactly 2100mm high from the same finished-floor datum. "
        "All door heads must align, and all doors must share identical frame thickness, casing width and leaf proportions. "
        "Never turn a door into a narrow decorative panel, cabinet front, half-height door or oversized portal. "
        "Every wall must remain one flat architectural plane: vertical wall and door edges must be straight in every "
        "rectilinear view. Never bow, bulge, ripple, fold or round a wall surface or corner. "
        "Every listed window must have a complete "
        "frame and glazing, never an empty wall slit. Do not invent extra room doors, hallway doors, balcony doors, "
        "windows, wall cutouts, closets, wardrobes, cabinet doors or cupboard doors. If an opening is not specified, "
        "keep that wall blank. Use few, simple pieces of furniture with clear walking space; no object may intersect "
        "another object or a wall, and no furniture may be duplicated across the wrapped seam. Use handleless cabinetry. "
        "All walls must use uniform, clean, "
        "solid-color matte latex paint with even coverage. Do not use microcement, limewash, stone texture, marbling, "
        "mottling, stains, patches, cloudy gradients or decorative wall texture. Use one physically consistent daylight "
        "direction with even exposure; do not create wall washers, unexplained light pools or dirty-looking shadows. "
        f"Style requirements: {style.promptSuffix} Preferred palette: {', '.join(style.palette)}. "
        f"Preferred materials: {', '.join(style.materials)}. Return a seamless 2:1 ERP."
    )
    idempotency_key = hashlib.sha256(
        source.read_bytes() + prompt.encode("utf-8") + f"{model}:{quality}:{size}".encode()
    ).hexdigest()
    session = _build_tuzi_image_session(requests, HTTPAdapter, Retry)
    try:
        with source.open("rb") as image:
            response = session.post(
                f"{os.environ.get('TUZI_BASE_URL', 'https://api.tu-zi.com').rstrip('/')}/v1/images/edits",
                headers={"Authorization": f"Bearer {key}", "Idempotency-Key": idempotency_key},
                data={"model": model, "prompt": prompt, "n": "1", "size": size, "quality": quality, "response_format": "url"},
                files={"image": (source.name, image, "image/jpeg")}, timeout=_tuzi_image_timeout(),
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
    finally:
        session.close()
    decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise RuntimeError("AI image decode failed")
    if abs(decoded.shape[1] / decoded.shape[0] - 2.0) > 0.05:
        raise RuntimeError("AI image is not a near-2:1 panorama")
    normalized_width = decoded.shape[1] - decoded.shape[1] % 2
    normalized = cv2.resize(decoded, (normalized_width, normalized_width // 2), interpolation=cv2.INTER_LANCZOS4)
    strip = 64
    for offset in range(strip):
        alpha = 0.5 * (1 + math.cos(math.pi * offset / strip))
        average = (normalized[:, offset].astype(np.float32) + normalized[:, -1 - offset].astype(np.float32)) / 2
        normalized[:, offset] = np.clip(normalized[:, offset] * (1 - alpha) + average * alpha, 0, 255)
        normalized[:, -1 - offset] = np.clip(normalized[:, -1 - offset] * (1 - alpha) + average * alpha, 0, 255)
    cv2.imwrite(str(output), normalized, [cv2.IMWRITE_JPEG_QUALITY, 94])
    return output


def _tuzi_image_settings() -> tuple[str, str, str]:
    return (
        (os.environ.get("TUZI_IMAGE_MODEL") or "gpt-image-2.5").strip(),
        (os.environ.get("TUZI_IMAGE_QUALITY") or "4k").strip(),
        (os.environ.get("TUZI_IMAGE_SIZE") or "4096x2048").strip(),
    )


def _build_tuzi_image_session(requests_module, adapter_type, retry_type):
    """Create the bounded image-2 session without changing global proxy state."""

    session = requests_module.Session()
    proxy = (
        os.environ.get("TUZI_HTTP_PROXY", "").strip()
        or os.environ.get("HTTPS_PROXY", "").strip()
        or os.environ.get("HTTP_PROXY", "").strip()
    )
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.trust_env = True
    session.mount("https://", adapter_type(max_retries=retry_type(
        total=5, connect=5, read=2, status=5, backoff_factor=1,
        respect_retry_after_header=True,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "POST")),
    )))
    return session


def _tuzi_image_timeout() -> tuple[int, int]:
    try:
        read_timeout = max(30, int(os.environ.get("TUZI_IMAGE_TIMEOUT_SECONDS", "300")))
    except ValueError:
        read_timeout = 300
    return (20, read_timeout)


def _qa_ai_opening_semantics(candidate: Path, expected: dict[str, int]) -> dict[str, Any]:
    """Use the configured review VLM to fail closed on visible opening drift."""

    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from .vision.provider import extract_output_text, to_data_uri

    schema = {
        "type": "object",
        "properties": {
            "doorCount": {"type": "integer", "minimum": 0, "maximum": 16},
            "windowCount": {"type": "integer", "minimum": 0, "maximum": 16},
            "narrowDoorCount": {"type": "integer", "minimum": 0, "maximum": 16},
            "doorHeadAlignment": {"type": "string", "enum": ["aligned", "misaligned", "uncertain"]},
            "wallsPlanar": {"type": "string", "enum": ["yes", "no", "uncertain"]},
            "doorScalePlausible": {"type": "string", "enum": ["yes", "no", "uncertain"]},
            "furnitureOverlap": {"type": "boolean"},
            "duplicateFurniture": {"type": "boolean"},
            "seamClean": {"type": "string", "enum": ["yes", "no", "uncertain"]},
        },
        "required": [
            "doorCount", "windowCount", "narrowDoorCount", "doorHeadAlignment",
            "wallsPlanar", "doorScalePlausible", "furnitureOverlap",
            "duplicateFurniture", "seamClean",
        ],
        "additionalProperties": False,
    }
    prompt = (
        "Inspect this 2x2 contact sheet containing four non-overlapping 90-degree views (yaw 0/90/180/270) from one "
        "complete room panorama. Count every distinct visible architectural door and window exactly once. A narrow door is visibly much narrower "
        "than an ordinary residential hinged door. Judge whether the top edges of all door frames share a consistent "
        "physical height and whether every door has plausible residential proportions after allowing for panorama "
        "projection. Treat walls as planar only when their vertical edges remain straight and their surfaces do not "
        "bulge, ripple or fold in the interactive panorama. Report furniture overlap when solid objects intersect, "
        "merge impossibly, pass through walls, become translucent/ghosted, or have stretched geometry; report duplicate "
        "furniture when an object or decoration is visibly repeated by generation or across the wrapped seam. The left "
        "and right edges must join without broken geometry or duplicated "
        "objects. If any judgment cannot be made confidently, use uncertain. Return only the schema JSON."
    )
    session = _build_tuzi_image_session(requests, HTTPAdapter, Retry)
    try:
        key = os.environ.get("TUZI_API_KEY", "").strip()
        model = os.environ.get("TUZI_VISION_REVIEW_MODEL", "").strip()
        if not key or not model:
            raise ValueError("semantic QA configuration missing")
        candidate_image = cv2.imread(str(candidate), cv2.IMREAD_COLOR)
        if candidate_image is None:
            raise ValueError("semantic QA candidate is unreadable")
        encoded, contact_sheet = cv2.imencode(
            ".jpg", _erp_review_contact_sheet(candidate_image), [cv2.IMWRITE_JPEG_QUALITY, 92]
        )
        if not encoded:
            raise ValueError("semantic QA contact sheet encoding failed")
        response = session.post(
            f"{os.environ.get('TUZI_BASE_URL', 'https://api.tu-zi.com').rstrip('/')}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": to_data_uri(contact_sheet.tobytes())}},
                ]}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "room_opening_qa_v1", "strict": True, "schema": schema,
                }},
                "max_tokens": 512,
            },
            timeout=_tuzi_image_timeout(),
        )
        response.raise_for_status()
        text = extract_output_text(response.json())
        if text is None:
            raise ValueError("semantic QA response missing")
        return _evaluate_opening_semantics(json.loads(text), expected, model)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, requests.RequestException):
        return {"status": "unavailable", "expected": expected, "reasonCodes": ["OPENING_SEMANTIC_QA_UNAVAILABLE"]}
    finally:
        session.close()


def _evaluate_opening_semantics(observed: dict, expected: dict[str, int], model: str = "test") -> dict[str, Any]:
    required = {
        "doorCount", "windowCount", "narrowDoorCount", "doorHeadAlignment",
        "wallsPlanar", "doorScalePlausible", "furnitureOverlap",
        "duplicateFurniture", "seamClean",
    }
    if set(observed) != required or any(
        isinstance(observed[key], bool) or not isinstance(observed[key], int) or not 0 <= observed[key] <= 16
        for key in ("doorCount", "windowCount", "narrowDoorCount")
    ) or observed["doorHeadAlignment"] not in {"aligned", "misaligned", "uncertain"} or any(
        observed[key] not in {"yes", "no", "uncertain"}
        for key in ("wallsPlanar", "doorScalePlausible", "seamClean")
    ) or any(
        not isinstance(observed[key], bool)
        for key in ("furnitureOverlap", "duplicateFurniture")
    ):
        raise ValueError("invalid opening semantic QA payload")
    reasons = []
    if observed["doorCount"] != expected["doorCount"]:
        reasons.append("AI_DOOR_COUNT_MISMATCH")
    if observed["windowCount"] != expected["windowCount"]:
        reasons.append("AI_WINDOW_COUNT_MISMATCH")
    if observed["narrowDoorCount"]:
        reasons.append("AI_NARROW_DOOR_RENDERED")
    if observed["doorCount"] > 1 and observed["doorHeadAlignment"] != "aligned":
        reasons.append("AI_DOOR_HEADS_MISALIGNED")
    if observed["wallsPlanar"] != "yes":
        reasons.append("AI_WALLS_NOT_PLANAR")
    if observed["doorScalePlausible"] != "yes":
        reasons.append("AI_DOOR_SCALE_IMPLAUSIBLE")
    if observed["furnitureOverlap"]:
        reasons.append("AI_FURNITURE_OVERLAP")
    if observed["duplicateFurniture"]:
        reasons.append("AI_DUPLICATE_FURNITURE")
    if observed["seamClean"] != "yes":
        reasons.append("AI_SEAM_BROKEN")
    return {
        "status": "rejected" if reasons else "accepted",
        "model": model,
        "expected": expected,
        "observed": observed,
        "reasonCodes": reasons or ["AI_OPENINGS_MATCH_EXPECTED"],
    }


def _opening_semantic_retry_instruction(semantic_qa: dict) -> str:
    expected = semantic_qa["expected"]
    observed = semantic_qa["observed"]
    return (
        " The previous attempt was rejected by visual QA: it rendered "
        f"{observed['doorCount']} doors and {observed['windowCount']} windows, but the structural reference requires "
        f"exactly {expected['doorCount']} doors and {expected['windowCount']} windows. Regenerate from the original "
        "structural reference. Remove every extra or narrow door-shaped panel, keep all unspecified areas as continuous "
        "blank painted wall, and align every door head. Keep every wall planar, use physically plausible door proportions, "
        "remove intersecting or duplicated furniture, and preserve a clean wrapped seam. This correction is mandatory."
    )


def _expected_room_opening_counts(layout: LayoutRevision, room) -> dict[str, int]:
    specs = _room_opening_specs(layout, room)
    return {
        "doorCount": sum(item[0] == "door" for item in specs),
        "windowCount": sum(item[0] == "window" for item in specs),
    }


def _opening_hint(layout: LayoutRevision, room) -> str:
    """Describe only the allowed architectural openings for this room."""

    allowed = _room_opening_specs(layout, room)
    if not allowed:
        return "Architectural openings allowed in this room: exactly 0 (all walls must remain solid)."
    counts = ", ".join(
        f"{kind}×{sum(item[0] == kind for item in allowed)}"
        for kind in sorted({item[0] for item in allowed})
    )
    dimensions = "; ".join(
        _opening_render_instruction(kind, opening_width, opening_height)
        for kind, opening_width, opening_height in allowed
    )
    return (
        f"Architectural openings allowed in this room: exactly {len(allowed)} total ({counts}); "
        f"required clear widths: {dimensions}; no others."
    )


def _room_opening_specs(layout: LayoutRevision, room) -> list[tuple[str, int, int]]:
    """Return the rendering-only opening whitelist for one room."""

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
            allowed.append((
                opening.type.value,
                round(_render_opening_width(opening)),
                round(_render_opening_height(opening)),
            ))
    return allowed


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
