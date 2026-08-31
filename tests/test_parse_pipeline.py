"""T08: parse pipeline orchestration, traceability and fail-safe status."""

from __future__ import annotations

from uuid import uuid4

import cv2
import numpy as np
import pytest

from pano_platform.contracts import CommandEnvelope, CommandName
from pano_platform.facade import (
    CommandFacade,
    CommandNotImplementedError,
    parse_command,
)
from pano_platform.floorplan.parse import (
    ParseRun,
    ParseStatus,
    RunDeadline,
    VisionMode,
    gate_input,
    new_trace_id,
    orchestrate,
    _run_vision,
    _VISION_CACHE,
)


def _png(width=64, height=40) -> bytes:
    image = np.full((height, width, 3), 255, np.uint8)
    image[10:30, 10:50] = (0, 0, 0)
    ok, encoded = cv2.imencode(".png", image)
    return encoded.tobytes()


def test_trace_id_is_unique_and_formatted() -> None:
    assert new_trace_id() != new_trace_id()
    assert len(new_trace_id()) == 36  # uuid


def test_gate_rejects_empty() -> None:
    stage = gate_input(b"")
    assert stage.status == "rejected"


def test_gate_accepts_valid_png() -> None:
    stage = gate_input(_png())
    assert stage.status == "accepted"
    assert stage.data["contentType"] == "image/png"


def test_orchestrate_cv_only_returns_needs_review_not_wrong_publish() -> None:
    # OFF mode: no vision; pipeline must NOT publish a wrong revision.
    run = orchestrate(_png(), vision_mode=VisionMode.OFF, project_id="p001")
    assert isinstance(run, ParseRun)
    assert run.traceId
    assert run.status in (ParseStatus.NEEDS_REVIEW, ParseStatus.REJECTED)
    assert run.rooms_published is False
    assert run.stages  # stage graph recorded


def test_orchestrate_no_vision_config_needs_review() -> None:
    run = orchestrate(_png(), vision_mode=VisionMode.ASSISTED, vision_config=None)
    assert run.status in (ParseStatus.NEEDS_REVIEW, ParseStatus.REJECTED, ParseStatus.FAILED)
    assert run.rooms_published is False


def test_deadline_cancels_run() -> None:
    # A zero-second deadline always exceeds immediately at the first check.
    run = orchestrate(_png(), vision_mode=VisionMode.OFF, deadline=RunDeadline(seconds=0.0))
    assert run.status is ParseStatus.CANCELLED
    assert any(stage.status == "cancelled" for stage in run.stages)


def test_every_stage_has_traceable_evidence() -> None:
    run = orchestrate(_png(), vision_mode=VisionMode.OFF)
    assert run.traceId
    for stage in run.stages:
        assert stage.stage
        assert stage.status in ("accepted", "needsReview", "rejected", "failed", "cancelled")


def test_facade_parse_command_dispatches(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))

    async def run() -> None:
        facade = CommandFacade()
        facade.register(CommandName.PARSE, parse_command)
        envelope = CommandEnvelope(
            requestId=uuid4(), command=CommandName.PARSE, projectId="p001",
            payload={"imageBytes": _png()},
        )
        task = await facade.dispatch(envelope)
        assert task.status == "ACCEPTED"

    import asyncio

    asyncio.run(run())


def test_facade_unregistered_command_raises() -> None:
    async def run() -> None:
        facade = CommandFacade()
        with pytest.raises(CommandNotImplementedError):
            await facade.dispatch(
                CommandEnvelope(requestId=uuid4(), command=CommandName.PARSE, projectId="p001")
            )

    import asyncio

    asyncio.run(run())


def test_vision_modes_enum() -> None:
    assert VisionMode.OFF.value == "off"
    assert VisionMode.SHADOW.value == "shadow"
    assert VisionMode.ASSISTED.value == "assisted"


def test_vision_stage_reuses_validated_draft_cache(monkeypatch) -> None:
    from pano_platform.vision.config import VisionConfig
    from pano_platform.vision.contracts import VlmDraft
    from pano_platform.vision.provider import VisionRunResult
    from pano_platform.vision import tuzi_responses

    calls = 0

    class FakeProvider:
        def __init__(self, config): pass
        def close(self): pass
        def parse(self, request, image_bytes):
            nonlocal calls
            calls += 1
            draft = VlmDraft.model_validate({
                "scaleCandidates": [],
                "rooms": [{
                    "type": "bedroom", "polygon": [[0.1, 0.1], [0.8, 0.1], [0.8, 0.8]],
                    "confidence": 0.8,
                }],
                "openings": [], "warnings": [],
            })
            return VisionRunResult(draft=draft, evidence={
                "normalizedDraftSha256": "n" * 64,
                "rawResponseSha256": "r" * 64,
            })

    monkeypatch.setattr(tuzi_responses, "TuziResponsesVisionProvider", FakeProvider)
    config = VisionConfig.model_validate({
        "baseUrl": "https://cache-test.invalid", "apiKey": "secret",
        "primaryModel": "test-model", "reviewModel": "test-model",
    })
    _VISION_CACHE.clear()
    first = _run_vision(_png(), config, "floorplan_parse_v1", VisionMode.ASSISTED)
    second = _run_vision(_png(), config, "floorplan_parse_v1", VisionMode.ASSISTED)
    assert calls == 1
    assert first.data["evidence"]["cacheHit"] is False
    assert second.data["evidence"]["cacheHit"] is True
    assert second.data["inputArtifact"]["processedSha256"]
