from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from pano_platform.floorplan.fusion_rooms import ClosedRoom
from pano_platform.floorplan.parse import ParseRun, ParseStatus, StageGraph, VisionMode
from pano_platform.floorplan.parse import orchestrate
from pano_platform.floorplan.wall_graph import WallGraph, WallSegment
from pano_platform.contracts import DerivedRoomSet, LayoutRevision, PanoManifest, SceneGeometry
from pano_platform.pipeline import _publish_preview, _render_panoramas


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


def test_ai_renders_every_room(tmp_path, monkeypatch) -> None:
    import shutil
    from pano_platform import pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    layout, rooms, _ = _publish_preview("upload-ai", b"fixture", _preview_run())

    def fake_ai(source: Path, output: Path, room_type: str, opening_hint: str = "") -> Path:
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
    manifest, ai_generated_count, failed_rooms, superres_count, superres_failed = _render_panoramas("upload-ai", layout, rooms, generate_ai=True)
    assert ai_generated_count == len(rooms.rooms) == 2
    assert failed_rooms == []
    assert superres_count == 2 and superres_failed == []
    assert all(scene.title.endswith("AI+超分预览") for scene in manifest.scenes)
    assert all(scene.assets.original.url.endswith("ai-erp-6144x3072.jpg") for scene in manifest.scenes)


def test_one_ai_failure_does_not_block_other_rooms(tmp_path, monkeypatch) -> None:
    import shutil
    from pano_platform import pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    layout, rooms, _ = _publish_preview("upload-partial", b"fixture", _preview_run())

    def partial_ai(source: Path, output: Path, room_type: str, opening_hint: str = "") -> Path:
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
