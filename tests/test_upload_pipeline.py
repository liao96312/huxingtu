from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from pano_platform.floorplan.fusion_rooms import ClosedRoom
from pano_platform.floorplan.parse import ParseRun, ParseStatus, StageGraph, VisionMode
from pano_platform.floorplan.parse import orchestrate
from pano_platform.floorplan.wall_graph import WallGraph, WallSegment
from pano_platform.contracts import DerivedRoomSet, LayoutRevision, PanoManifest, SceneGeometry, canonical_sha256
from pano_platform.pipeline import _build_room_hotspots, _publish_preview, _render_panoramas


def _preview_run() -> ParseRun:
    segments = (
        WallSegment("wall_top", "horizontal", (10, 10), (110, 10), 10, False),
        WallSegment("wall_right", "vertical", (110, 10), (110, 90), 10, False),
        WallSegment("wall_bottom", "horizontal", (110, 90), (10, 90), 10, False),
        WallSegment("wall_left", "vertical", (10, 90), (10, 10), 10, False),
    )
    graph = WallGraph((10, 90), (10, 110), segments, (), 10.0)
    room = ClosedRoom(
        "room_001", "livingDining", ((10, 10), (60, 10), (60, 90), (10, 90)),
        4_000_000, True, True, (),
    )
    room_2 = ClosedRoom(
        "room_002", "bedroom", ((60, 10), (110, 10), (110, 90), (60, 90)),
        4_000_000, True, True, (),
    )
    fusion = StageGraph(
        "fusion", "needsReview", "preview fixture",
        {"wallGraph": graph, "mmPerPx": 10.0, "fusedRooms": (room, room_2), "confirmedOpenings": ()},
    )
    return ParseRun(
        "11111111-1111-1111-1111-111111111111", ParseStatus.NEEDS_REVIEW,
        (fusion,), VisionMode.ASSISTED, True, False, False, ("ROOM_QUALITY_NOT_CONFIRMED",),
    )


def test_preview_geometry_and_erp_are_published(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    layout, rooms, scene = _publish_preview("upload-test", b"fixture", _preview_run())
    manifest, ai_generated_count, failed_rooms, superres_count, superres_failed = _render_panoramas("upload-test", layout, rooms, generate_ai=False)

    assert len(layout.edges) == 4
    assert len(rooms.rooms) == 2
    assert scene.wallSlabs
    assert manifest.scenes[0].assets.original.width == 2048
    assert manifest.scenes[0].assets.original.height == 1024
    assert ai_generated_count == 0 and failed_rooms == []
    assert superres_count == 0 and superres_failed == []
    panorama = tmp_path / "upload-test/panorama/room_001-main/structural-erp-2048x1024.jpg"
    assert panorama.is_file() and panorama.stat().st_size > 1000
    folder = tmp_path / "upload-test"
    LayoutRevision.model_validate_json((folder / "layout-revision.preview-v1.json").read_text(encoding="utf-8"))
    DerivedRoomSet.model_validate_json((folder / "rooms.preview-v1.json").read_text(encoding="utf-8"))
    SceneGeometry.model_validate_json((folder / "scene-geometry.preview-v1.json").read_text(encoding="utf-8"))
    PanoManifest.model_validate_json((folder / "pano-manifest.json").read_text(encoding="utf-8"))


def test_door_hotspots_link_only_two_nearby_rooms() -> None:
    layout_payload = {
        "schemaVersion": "0.1.0", "id": "layout", "projectId": "p", "revision": 1,
        "units": "mm", "coordinateFrame": "business-z-up-rh",
        "source": {"imageArtifactId": "sha256:" + "1" * 64, "pixelToWorldTransform": (1, 0, 0, 0, 1, 0, 0, 0, 1), "scaleConfidence": 0.5},
        "nodes": [
            {"id": "n1", "x": 0, "y": 0}, {"id": "n2", "x": 1000, "y": 0},
            {"id": "n3", "x": 1000, "y": 1000}, {"id": "n4", "x": 0, "y": 1000},
            {"id": "n5", "x": 2000, "y": 0}, {"id": "n6", "x": 2000, "y": 1000},
        ],
        "edges": [
            {"id": "wall-top", "startNodeId": "n1", "endNodeId": "n2", "thickness": 120, "height": 2800, "confidence": 0.5},
            {"id": "wall-mid", "startNodeId": "n2", "endNodeId": "n3", "thickness": 120, "height": 2800, "confidence": 0.5},
            {"id": "wall-bottom", "startNodeId": "n3", "endNodeId": "n4", "thickness": 120, "height": 2800, "confidence": 0.5},
            {"id": "wall-left", "startNodeId": "n4", "endNodeId": "n1", "thickness": 120, "height": 2800, "confidence": 0.5},
            {"id": "wall-right", "startNodeId": "n2", "endNodeId": "n5", "thickness": 120, "height": 2800, "confidence": 0.5},
            {"id": "wall-right2", "startNodeId": "n5", "endNodeId": "n6", "thickness": 120, "height": 2800, "confidence": 0.5},
        ],
        "openings": [{"id": "door-1", "type": "door", "wallEdgeId": "wall-mid", "offset": 400, "width": 200, "height": 2200, "confidence": 0.5}],
        "roomAnnotations": [],
    }
    layout_payload["contentHash"] = canonical_sha256(layout_payload)
    layout = LayoutRevision.model_validate(layout_payload)
    room_payload = {
        "schemaVersion": "0.1.0", "projectId": "p", "layoutRevisionId": "layout", "sourceLayoutHash": "2" * 64,
        "rooms": [
            {"id": "a", "name": "客厅", "type": "living", "polygon": [{"x": 50, "y": 50}, {"x": 950, "y": 50}, {"x": 950, "y": 950}, {"x": 50, "y": 950}], "areaMm2": 810000, "confidence": 0.5},
            {"id": "b", "name": "卧室", "type": "bedroom", "polygon": [{"x": 1050, "y": 50}, {"x": 1950, "y": 50}, {"x": 1950, "y": 950}, {"x": 1050, "y": 950}], "areaMm2": 810000, "confidence": 0.5},
        ],
    }
    room_payload["contentHash"] = canonical_sha256(room_payload)
    rooms = DerivedRoomSet.model_validate(room_payload)
    hotspots = _build_room_hotspots(layout, rooms, {"a": "a-main", "b": "b-main"})
    assert hotspots["a"][0]["targetSceneId"] == "b-main"
    assert hotspots["b"][0]["targetSceneId"] == "a-main"


def test_cv_only_run_still_builds_automatic_preview(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    image_path = Path(__file__).parents[1] / "tests/fixtures/floorplan-golden/fp-golden.png"
    payload = image_path.read_bytes()
    run = orchestrate(payload, vision_mode=VisionMode.OFF, project_id="cv-only")
    layout, rooms, scene = _publish_preview("cv-only", payload, run)
    assert run.status is ParseStatus.NEEDS_REVIEW
    assert layout.id == "cv-only_preview_r1"
    assert rooms.rooms
    assert scene.solids


def test_weak_recognition_still_publishes_envelope_preview(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    run = ParseRun(
        "22222222-2222-2222-2222-222222222222", ParseStatus.FAILED,
        (StageGraph("input_gate", "accepted", "image/png 1200x800"),
         StageGraph("cv_structure", "failed", "wall detector unavailable")),
        VisionMode.OFF, True, False, False, ("CV_STRUCTURE_FAILED",),
    )
    layout, rooms, scene = _publish_preview("weak-recognition", b"not-a-real-image", run)

    assert len(rooms.rooms) == 1
    assert rooms.rooms[0].type == "other"
    assert layout.source.scaleConfidence == 0.1
    assert len(layout.edges) == 4
    assert scene.solids
    evidence = tmp_path / "weak-recognition/automatic-fallback.json"
    assert evidence.is_file()
    assert "AUTO_ENVELOPE_FALLBACK" in evidence.read_text(encoding="utf-8")


def test_rejected_input_does_not_get_an_envelope(tmp_path, monkeypatch) -> None:
    from pano_platform.pipeline import run_upload_pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    result = run_upload_pipeline(
        b"not-a-real-image", project_id="invalid-upload",
        allow_external_vision=False, generate_ai=False,
    )

    assert result["pipelineStatus"] == "failed"
    assert result["viewerUrl"] is None


def test_pipeline_reports_monotonic_automatic_progress(tmp_path, monkeypatch) -> None:
    from pano_platform.pipeline import run_upload_pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    image = (Path(__file__).parents[1] / "tests/fixtures/floorplan-golden/fp-golden.png").read_bytes()
    updates: list[tuple[int, str]] = []
    result = run_upload_pipeline(
        image, project_id="progress-check", allow_external_vision=False,
        generate_ai=False, progress_callback=lambda value, stage: updates.append((value, stage)),
    )

    assert result["viewerUrl"]
    assert updates[-1] == (100, "done")
    assert [value for value, _ in updates] == sorted(value for value, _ in updates)
    assert {stage for _, stage in updates} >= {"parse", "geometry", "structure-preview", "panorama-complete", "done"}


def test_one_structural_room_failure_does_not_block_other_rooms(tmp_path, monkeypatch) -> None:
    from pano_platform import pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    layout, rooms, _ = _publish_preview("structural-partial", b"fixture", _preview_run())
    original = pipeline.render_structural_erp

    def partial_render(layout_arg, room_arg, width):
        if room_arg["id"] == "room_001":
            raise RuntimeError("degenerate room")
        return original(layout_arg, room_arg, width)

    monkeypatch.setattr(pipeline, "render_structural_erp", partial_render)
    manifest, _, _, _, _ = _render_panoramas("structural-partial", layout, rooms, generate_ai=False)

    assert len(manifest.scenes) == 2
    evidence = tmp_path / "structural-partial/structural-fallback-rooms.json"
    assert evidence.is_file() and "客餐厅" in evidence.read_text(encoding="utf-8")


def test_ai_renders_every_room(tmp_path, monkeypatch) -> None:
    import shutil
    from pano_platform import pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    layout, rooms, _ = _publish_preview("upload-ai", b"fixture", _preview_run())

    styles_seen = []

    def fake_ai(source: Path, output: Path, room_type: str, opening_hint: str = "", style_context=None) -> Path:
        styles_seen.append(style_context.id)
        shutil.copy2(source, output)
        return output

    def fake_superres(source: Path, folder: Path) -> dict[str, Path]:
        paths = {key: folder / name for key, name in {
            "original": "ai-erp-6144x3072.jpg", "proxy4k": "ai-erp-proxy-4096x2048.jpg",
            "proxy2k": "ai-erp-proxy-2048x1024.jpg", "proxy1k": "ai-erp-proxy-1024x512.jpg",
        }.items()}
        for path in paths.values(): shutil.copy2(source, path)
        return paths

    monkeypatch.setattr(pipeline, "_generate_ai_panorama", fake_ai)
    monkeypatch.setattr(pipeline, "_upscale_with_realesrgan", fake_superres)
    manifest, ai_generated_count, failed_rooms, superres_count, superres_failed = _render_panoramas(
        "upload-ai", layout, rooms, generate_ai=True, style_id="cream"
    )
    assert ai_generated_count == len(rooms.rooms) == 2
    assert failed_rooms == []
    assert superres_count == 2 and superres_failed == []
    assert all(scene.title.endswith("AI+超分预览") for scene in manifest.scenes)
    assert all(scene.assets.original.url.endswith("ai-erp-6144x3072.jpg") for scene in manifest.scenes)
    assert styles_seen == ["cream", "cream"]
    assert "奶油风" in manifest.share.title


def test_one_ai_failure_does_not_block_other_rooms(tmp_path, monkeypatch) -> None:
    import shutil
    from pano_platform import pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    layout, rooms, _ = _publish_preview("upload-partial", b"fixture", _preview_run())

    def partial_ai(source: Path, output: Path, room_type: str, opening_hint: str = "", style_context=None) -> Path:
        if room_type == "bedroom":
            raise RuntimeError("provider unavailable")
        shutil.copy2(source, output)
        return output

    def fake_superres(source: Path, folder: Path) -> dict[str, Path]:
        paths = {key: folder / name for key, name in {
            "original": "ai-erp-6144x3072.jpg", "proxy4k": "ai-erp-proxy-4096x2048.jpg",
            "proxy2k": "ai-erp-proxy-2048x1024.jpg", "proxy1k": "ai-erp-proxy-1024x512.jpg",
        }.items()}
        for path in paths.values(): shutil.copy2(source, path)
        return paths

    monkeypatch.setattr(pipeline, "_generate_ai_panorama", partial_ai)
    monkeypatch.setattr(pipeline, "_upscale_with_realesrgan", fake_superres)
    manifest, count, failed, superres_count, superres_failed = _render_panoramas("upload-partial", layout, rooms, generate_ai=True)
    assert count == 1
    assert failed == ["卧室"]
    assert superres_count == 1 and superres_failed == []
    assert sum(scene.title.endswith("AI+超分预览") for scene in manifest.scenes) == 1


def test_ai_structure_gate_rejects_blank_candidate(tmp_path, monkeypatch) -> None:
    import cv2
    import numpy as np
    from pano_platform.pipeline import _qa_ai_panorama_structure

    reference = tmp_path / "reference.jpg"
    candidate = tmp_path / "candidate.jpg"
    source = np.full((256, 512, 3), 235, dtype=np.uint8)
    cv2.rectangle(source, (40, 30), (470, 220), (80, 80, 80), 8)
    cv2.imwrite(str(reference), source)
    cv2.imwrite(str(candidate), np.full_like(source, 235))

    qa = _qa_ai_panorama_structure(reference, candidate)
    assert qa["status"] == "rejected"
    assert "STRUCTURE_EDGE_RECALL_LOW" in qa["reasonCodes"]
    assert qa["semanticIoU"] is None and qa["semanticF1"] is None


def test_ai_structure_gate_rejects_excessive_long_lines(tmp_path) -> None:
    import cv2
    import numpy as np
    from pano_platform.pipeline import _qa_ai_panorama_structure

    reference = tmp_path / "reference.jpg"
    candidate = tmp_path / "candidate.jpg"
    source = np.full((256, 512, 3), 235, dtype=np.uint8)
    cv2.rectangle(source, (40, 30), (470, 220), (80, 80, 80), 8)
    noisy = source.copy()
    for x in np.linspace(5, 505, 50, dtype=int):
        cv2.line(noisy, (x, 75), (x, 230), (20, 20, 20), 4)
    cv2.imwrite(str(reference), source)
    cv2.imwrite(str(candidate), noisy)

    qa = _qa_ai_panorama_structure(reference, candidate)
    assert qa["status"] == "rejected"
    assert "STRUCTURE_LONG_LINE_DENSITY_DRIFT" in qa["reasonCodes"]


def test_upload_endpoint_returns_pollable_task(monkeypatch) -> None:
    from pano_platform import main

    monkeypatch.setattr(main, "run_upload_pipeline", lambda image, **kwargs: {
        "projectId": kwargs["project_id"], "pipelineStatus": "previewReady",
        "parseStatus": "needsReview", "stages": [],
        "viewerUrl": "/?manifestUrl=/artifacts/upload-test/pano-manifest.json",
    })

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/v1/pipeline/upload",
                data={"projectId": "upload-test", "allowExternalVision": "false"},
                files={"file": ("plan.png", b"fake-png", "image/png")},
            )

    response = asyncio.run(run())
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "PROCESSING"
    assert payload["pollUrl"].startswith("/api/v1/pipeline/tasks/")
    import time

    time.sleep(0.05)
    async def poll() -> httpx.Response:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(payload["pollUrl"])

    task = asyncio.run(poll())
    assert task.status_code == 200
    assert task.json()["status"] == "DONE"
