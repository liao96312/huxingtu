from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx

from pano_platform.floorplan.fusion_rooms import ClosedRoom
from pano_platform.floorplan.parse import ParseRun, ParseStatus, StageGraph, VisionMode
from pano_platform.floorplan.parse import orchestrate
from pano_platform.floorplan.wall_graph import WallGraph, WallSegment
from pano_platform.contracts import DerivedRoomSet, LayoutRevision, PanoManifest, SceneGeometry, canonical_sha256
from pano_platform.pipeline import (
    _build_room_hotspots, _choose_erp_seam_yaw, _enclosing_room_map, _opening_hint,
    _opening_render_instruction, _publish_preview, _render_opening_height,
    _evaluate_opening_semantics, _opening_semantic_retry_instruction,
    _render_opening_width, _render_panoramas, _tuzi_image_settings,
    _wall_cleanliness_proxy,
)


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
    room_evidence = json.loads((folder / "automatic-room-evidence.json").read_text(encoding="utf-8"))
    assert room_evidence["unsupportedBoundaryRoomIds"] == []
    assert room_evidence["unsupportedWallRoomIds"] == []
    assert room_evidence["implausiblySmallRoomIds"] == []
    assert room_evidence["suspectedEnclosingRooms"] == []


def test_image_25_4k_is_the_default_with_environment_override(monkeypatch) -> None:
    for name in ("TUZI_IMAGE_MODEL", "TUZI_IMAGE_QUALITY", "TUZI_IMAGE_SIZE"):
        monkeypatch.delenv(name, raising=False)
    assert _tuzi_image_settings() == ("gpt-image-2.5", "4k", "4096x2048")
    monkeypatch.setenv("TUZI_IMAGE_MODEL", "gpt-image-2")
    assert _tuzi_image_settings()[0] == "gpt-image-2"


def test_wall_cleanliness_rejects_cloudy_painted_wall() -> None:
    import cv2
    import numpy as np

    reference = np.full((256, 512, 3), 220, np.uint8)
    candidate = reference.copy()
    for x in range(40, 500, 80):
        cv2.circle(candidate, (x, 120), 28, (145, 145, 145), -1)
    result = _wall_cleanliness_proxy(reference, candidate, "livingDining")
    assert result["status"] == "rejected"
    assert result["lowFrequencyUnevennessP90"] > result["threshold"]


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
    assert _render_opening_width(layout.openings[0]) == 900
    assert _render_opening_width(layout.openings[0].model_copy(update={"width": 1371})) == 900
    assert _render_opening_height(layout.openings[0].model_copy(update={"height": 2600})) == 2100
    assert "door opening exactly 900mm wide and 2100mm high" in _opening_hint(layout, rooms.rooms[0])
    assert abs(_choose_erp_seam_yaw(layout, {node.id: node for node in layout.nodes}, 500, 500) - 3.14159) < 0.01
    hotspots = _build_room_hotspots(layout, rooms, {"a": "a-main", "b": "b-main"})
    assert hotspots["a"][0]["targetSceneId"] == "b-main"
    assert hotspots["b"][0]["targetSceneId"] == "a-main"


def test_opening_semantic_qa_rejects_extra_door() -> None:
    result = _evaluate_opening_semantics({
        "doorCount": 3,
        "windowCount": 2,
        "narrowDoorCount": 0,
        "doorHeadAlignment": "aligned",
    }, {"doorCount": 2, "windowCount": 2})
    assert result["status"] == "rejected"
    assert result["reasonCodes"] == ["AI_DOOR_COUNT_MISMATCH"]
    retry = _opening_semantic_retry_instruction(result)
    assert "rendered 3 doors and 2 windows" in retry
    assert "exactly 2 doors and 2 windows" in retry


def test_enclosing_shell_is_identified_without_deleting_rooms() -> None:
    from types import SimpleNamespace

    def room(identifier, points):
        return SimpleNamespace(
            id=identifier,
            polygon=[SimpleNamespace(x=x, y=y) for x, y in points],
        )

    shell = room("shell", ((0, 0), (100, 0), (100, 100), (0, 100)))
    left = room("left", ((10, 10), (45, 10), (45, 90), (10, 90)))
    right = room("right", ((55, 10), (90, 10), (90, 90), (55, 90)))

    assert _enclosing_room_map([shell, left, right]) == {
        "shell": ("left", "right")
    }


def test_tuzi_image_session_uses_explicit_proxy_without_global_mutation(monkeypatch) -> None:
    from pano_platform.pipeline import _build_tuzi_image_session
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    monkeypatch.setenv("TUZI_HTTP_PROXY", "http://127.0.0.1:7897")
    session = _build_tuzi_image_session(requests, HTTPAdapter, Retry)
    try:
        assert session.proxies["https"] == "http://127.0.0.1:7897"
        assert session.adapters["https://"].max_retries.total == 5
    finally:
        session.close()


def test_pipeline_task_is_reloaded_and_resubmitted_after_memory_loss(tmp_path, monkeypatch) -> None:
    from pano_platform import main

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    task_id = uuid4()
    task = {
        "taskId": str(task_id), "projectId": "restart-check", "status": "PROCESSING",
        "progress": 42, "stage": "geometry", "allowExternalVision": False,
        "generateAi": False, "styleId": "modern", "result": None, "error": None,
    }
    main._persist_pipeline_task(task_id, task, b"persisted-image")
    main._pipeline_tasks.clear()
    main._pipeline_active.clear()
    submitted = []
    monkeypatch.setattr(main._pipeline_executor, "submit", lambda function, *args: submitted.append((function, args)))
    loaded = asyncio.run(main.get_pipeline_task(task_id))
    try:
        assert loaded["progress"] == 42
        assert submitted and submitted[0][1][0] == task_id
        assert submitted[0][1][1] == b"persisted-image"
    finally:
        main._pipeline_active.clear()


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


def test_connected_window_panes_survive_into_automatic_pipeline(tmp_path, monkeypatch) -> None:
    from pano_platform.pipeline import run_upload_pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    image_path = Path(__file__).parents[1] / "datasets/source/desktop-9/fp-109.png"
    result = run_upload_pipeline(
        image_path.read_bytes(), project_id="window-ink", allow_external_vision=False,
        generate_ai=False,
    )

    # Full-axis pane scanning recovers the second visible top-right balcony window.
    assert result["automaticCvWindowCount"] == 2


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
    assert any(stage.startswith("panorama-room-") for _, stage in updates)


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
    monkeypatch.setattr(pipeline, "_qa_ai_opening_semantics", lambda candidate, expected: {"status": "accepted", "reasonCodes": []})
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


def test_opening_mismatch_gets_one_corrective_regeneration(tmp_path, monkeypatch) -> None:
    import shutil
    from pano_platform import pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    layout, rooms, _ = _publish_preview("upload-retry", b"fixture", _preview_run())
    rooms = rooms.model_copy(update={"rooms": rooms.rooms[:1]})
    hints = []
    semantic_results = []
    semantic_calls = 0

    def fake_ai(source, output, room_type, opening_hint="", style_context=None):
        hints.append(opening_hint)
        shutil.copy2(source, output)
        return output

    def fake_semantic(candidate, expected):
        nonlocal semantic_calls
        semantic_calls += 1
        observed = {
            "doorCount": 3 if semantic_calls == 1 else expected["doorCount"],
            "windowCount": expected["windowCount"],
            "narrowDoorCount": 0,
            "doorHeadAlignment": "aligned",
        }
        result = _evaluate_opening_semantics(observed, expected)
        semantic_results.append(result)
        return result

    def fake_superres(source, folder):
        paths = {key: folder / name for key, name in {
            "original": "ai-erp-8192x4096.jpg", "proxy4k": "ai-erp-proxy-4096x2048.jpg",
            "proxy2k": "ai-erp-proxy-2048x1024.jpg", "proxy1k": "ai-erp-proxy-1024x512.jpg",
        }.items()}
        for path in paths.values():
            shutil.copy2(source, path)
        return paths

    monkeypatch.setattr(pipeline, "_generate_ai_panorama", fake_ai)
    monkeypatch.setattr(pipeline, "_qa_ai_panorama_structure", lambda *args: {"status": "accepted", "reasonCodes": []})
    monkeypatch.setattr(pipeline, "_qa_ai_opening_semantics", fake_semantic)
    monkeypatch.setattr(pipeline, "_opening_semantic_retry_instruction", lambda qa: " previous attempt was rejected")
    monkeypatch.setattr(pipeline, "_upscale_with_realesrgan", fake_superres)

    _, count, failed, superres_count, _ = _render_panoramas("upload-retry", layout, rooms, generate_ai=True)
    assert semantic_calls == 2, semantic_results
    assert len(hints) == 2
    assert "previous attempt was rejected" in hints[1]
    assert count == superres_count == 1 and failed == []


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
    monkeypatch.setattr(pipeline, "_qa_ai_opening_semantics", lambda candidate, expected: {"status": "accepted", "reasonCodes": []})
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
