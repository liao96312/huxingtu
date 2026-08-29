from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from pano_platform.floorplan.fusion_rooms import ClosedRoom
from pano_platform.floorplan.parse import ParseRun, ParseStatus, StageGraph, VisionMode
from pano_platform.floorplan.wall_graph import WallGraph, WallSegment
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
        "room_001", "livingDining", ((10, 10), (110, 10), (110, 90), (10, 90)),
        8_000_000, True, True, (),
    )
    fusion = StageGraph(
        "fusion", "needsReview", "preview fixture",
        {"wallGraph": graph, "mmPerPx": 10.0, "fusedRooms": (room,), "confirmedOpenings": ()},
    )
    return ParseRun(
        "11111111-1111-1111-1111-111111111111", ParseStatus.NEEDS_REVIEW,
        (fusion,), VisionMode.ASSISTED, True, False, False, ("ROOM_QUALITY_NOT_CONFIRMED",),
    )


def test_preview_geometry_and_erp_are_published(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    layout, rooms, scene = _publish_preview("upload-test", b"fixture", _preview_run())
    manifest, ai_generated, warning = _render_panoramas("upload-test", layout, rooms, generate_ai=False)

    assert len(layout.edges) == 4
    assert len(rooms.rooms) == 1
    assert scene.wallSlabs
    assert manifest.scenes[0].assets.original.width == 2048
    assert manifest.scenes[0].assets.original.height == 1024
    assert ai_generated is False and warning is None
    panorama = tmp_path / "upload-test/panorama/room_001-main/structural-erp-2048x1024.jpg"
    assert panorama.is_file() and panorama.stat().st_size > 1000


def test_optional_ai_replaces_only_main_scene(tmp_path, monkeypatch) -> None:
    import shutil
    from pano_platform import pipeline

    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    layout, rooms, _ = _publish_preview("upload-ai", b"fixture", _preview_run())

    def fake_ai(source: Path, output: Path, room_type: str) -> Path:
        shutil.copy2(source, output)
        return output

    monkeypatch.setattr(pipeline, "_generate_ai_panorama", fake_ai)
    manifest, ai_generated, warning = _render_panoramas("upload-ai", layout, rooms, generate_ai=True)
    assert ai_generated is True and warning is None
    assert manifest.scenes[0].title.endswith("AI预览")
    assert manifest.scenes[0].assets.original.url.endswith("ai-erp-2048x1024.jpg")


def test_upload_endpoint_returns_viewer_link(monkeypatch) -> None:
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
    assert response.status_code == 200
    assert response.json()["viewerUrl"].startswith("/?manifestUrl=/artifacts/")
