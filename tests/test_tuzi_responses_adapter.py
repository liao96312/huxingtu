"""T03: Tuzi Responses adapter normalisation and retry behaviour.

All tests run against ``httpx.MockTransport`` — no real network and no API key.
A single real smoke test runs only when ``TUZI_API_KEY`` is actually present in
the environment, and is skipped in CI (the default here).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from pano_platform.vision.config import VisionConfig
from pano_platform.vision.contracts import (
    VisionParseRequest,
    VlmDraft,
)
from pano_platform.vision.provider import VisionError
from pano_platform.vision.tuzi_responses import TuziResponsesVisionProvider

ROOT = Path(__file__).parents[1]

_SENTINEL = object()


def config(**overrides) -> VisionConfig:
    values = {
        "baseUrl": "https://api.tu-zi.com",
        "apiKey": "sk-secret-1234567890abcdef",
        "primaryModel": "gemini-3.5-flash",
        "reviewModel": "gemini-3.5-flash",
        "timeoutSeconds": 10,
    }
    values.update(overrides)
    return VisionConfig.model_validate(values)


def parse_request(model: str = "gemini-3.5-flash", image_sha256: str = "a" * 64) -> VisionParseRequest:
    return VisionParseRequest(
        imageSha256=image_sha256, provider="tuzi", model=model,
        promptVersion="floorplan_parse_v1", draftSchemaVersion="0.1.0",
        sourceWidth=799, sourceHeight=799,
    )


def valid_draft_text() -> str:
    return json.dumps({
        "scaleCandidates": [{"lengthMm": 3600, "pixelStart": [0.3, 0.88], "pixelEnd": [0.7, 0.88], "text": "3600", "confidence": 0.94}],
        "rooms": [{"type": "bedroom", "name": "卧室", "areaLabelM2": 12.8,
                   "polygon": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]], "confidence": 0.9}],
        "openings": [{"type": "door", "evidence": ["doorArc"], "pixelStart": [0.3, 0.25], "pixelEnd": [0.3, 0.35], "confidence": 0.88}],
        "warnings": [],
    })


def responses_response(text: str, model="gemini-3.5-flash", request_id="req_1"):
    return {
        "id": request_id,
        "model": model,
        "object": "chat.completion",
        "usage": {"prompt_tokens": 100, "completion_tokens": 42, "total_tokens": 142},
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


def transport_with(*statuses, text=_SENTINEL, model="gemini-3.5-flash"):
    """Return a handler that plays a queued sequence of HTTP statuses then a success."""

    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "sk-secret" not in str(request.url)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemini-3.5-flash"}]})
        assert "sk-secret" not in (request.content or b"").decode("utf-8", "replace")
        if queue:
            status = queue.pop(0)
            return httpx.Response(status, json={"error": {"code": "rate_limit"}})
        if text is _SENTINEL:
            # default: a valid draft so success-path tests get a parseable payload
            return httpx.Response(200, json=responses_response(valid_draft_text(), model=model))
        if text is None:
            # explicit None: a successful completion that carries no text content
            return httpx.Response(200, json={"id": "req_1", "model": model, "choices": [{"index": 0, "message": {"role": "assistant"}, "finish_reason": "length"}]})
        return httpx.Response(200, json=responses_response(text, model=model))

    return httpx.MockTransport(handler)


def test_parse_success_records_evidence() -> None:
    provider = TuziResponsesVisionProvider(config(), httpx.Client(transport=transport_with()))
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()

    assert result.error is None
    assert isinstance(result.draft, VlmDraft)
    assert len(result.draft.rooms) == 1
    assert result.evidence["provider"] == "tuzi"
    assert result.evidence["endpoint"] == "/v1/chat/completions"
    assert result.evidence["providerRequestId"] == "req_1"
    assert result.evidence["actualModel"] == "gemini-3.5-flash"
    assert result.evidence["latencyMs"] >= 0
    assert result.evidence["usage"]["completion_tokens"] == 42
    assert result.evidence["normalizedDraftSha256"]
    assert "sk-secret" not in json.dumps(result.evidence)


def test_429_is_retried_then_succeeds() -> None:
    provider = TuziResponsesVisionProvider(config(), httpx.Client(transport=transport_with(429)))
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()
    assert result.error is None
    assert result.evidence["attemptCount"] >= 2


def test_5xx_then_success_retries() -> None:
    provider = TuziResponsesVisionProvider(config(), httpx.Client(transport=transport_with(503)))
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()
    assert result.error is None


def test_5xx_exhausted_returns_upstream_error() -> None:
    provider = TuziResponsesVisionProvider(config(), httpx.Client(transport=transport_with(503, 502, 504, 500, 500)))
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()
    assert result.error is VisionError.VISION_UPSTREAM_ERROR
    assert result.draft is None


def test_auth_error_is_not_retried() -> None:
    provider = TuziResponsesVisionProvider(config(), httpx.Client(transport=transport_with(403)))
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()
    assert result.error is VisionError.VISION_PROVIDER_UNAVAILABLE
    assert result.evidence["attemptCount"] == 1


def test_model_not_found_is_distinct() -> None:
    provider = TuziResponsesVisionProvider(config(), httpx.Client(transport=transport_with(404)))
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()
    assert result.error is VisionError.VISION_MODEL_NOT_AVAILABLE


def test_truncated_garbage_is_schema_invalid() -> None:
    provider = TuziResponsesVisionProvider(
        config(), httpx.Client(transport=transport_with(text='{"rooms": [{"type": "bedro'))
    )
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()
    assert result.error is VisionError.VISION_SCHEMA_INVALID


def test_markdown_json_is_not_guessed() -> None:
    # Provider must return raw JSON; a fenced block is NOT parsed.
    provider = TuziResponsesVisionProvider(
        config(), httpx.Client(transport=transport_with(text='```json\n{"rooms": []}\n```'))
    )
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()
    assert result.error is VisionError.VISION_SCHEMA_INVALID


def test_no_output_text_is_structured_output_unsupported() -> None:
    provider = TuziResponsesVisionProvider(
        config(), httpx.Client(transport=transport_with(text=None))
    )
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()
    assert result.error is VisionError.VISION_STRUCTURED_OUTPUT_UNSUPPORTED


def test_timeout_is_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    provider = TuziResponsesVisionProvider(config(), httpx.Client(transport=httpx.MockTransport(handler)))
    result = provider.parse(parse_request(), b"\x00" * 8)
    provider.close()
    assert result.error is VisionError.VISION_TIMEOUT
    assert result.evidence["attemptCount"] >= 1


@pytest.mark.skipif(
    os.environ.get("RUN_TUZI_LIVE_TEST") != "1"
    or not (os.environ.get("TUZI_API_KEY") or os.environ.get("TUZI_API_KEY_FILE")),
    reason="set RUN_TUZI_LIVE_TEST=1 with a Tuzi key to run paid live smoke",
)
def test_real_smoke_parse_on_floorplan() -> None:
    """Real smoke: parse an actual floorplan through the live Tuzi endpoint.

    Uses the strict VlmDraft schema (auto-loaded by the adapter) so a real
    candidate comes out. The image is fp-101 from the recovered desktop set
    (already authorised for external vision). If the drawing is parseable it
    should produce rooms/openings, or at worst an explicit warning -- never a
    schema error on a genuine floorplan.
    """
    from pathlib import Path

    from pano_platform.vision.config import VisionConfig
    from pano_platform.vision.tuzi_responses import TuziResponsesVisionProvider

    config = VisionConfig.from_env()
    image = (Path(__file__).parents[1] / "datasets/source/desktop-9/fp-101.png").read_bytes()
    provider = TuziResponsesVisionProvider(config)
    try:
        import hashlib

        result = provider.parse(
            parse_request(config.primaryModel, hashlib.sha256(image).hexdigest()), image
        )
        assert result.error is None
        assert result.draft is not None
    finally:
        provider.close()
