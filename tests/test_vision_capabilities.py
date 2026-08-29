"""T01: Tuzi vision capability discovery and probe normalisation.

These tests run entirely against ``httpx.MockTransport`` — no network call, no
real API key. They verify that the provider probes normalise failures to the
declared ``ProbeStatus`` enums and never leak the key or payload into the
recorded profile.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from pano_platform.vision.capabilities import (
    ProbeStatus,
    VisionCapabilityProfile,
    probe_model,
)
from pano_platform.vision.config import VisionConfig


def make_config(**overrides) -> VisionConfig:
    values = {
        "baseUrl": "https://api.tu-zi.com",
        "apiKey": "sk-secret-1234567890abcdef",
        "primaryModel": "gemini-3.5-flash",
        "reviewModel": "gemini-3.5-flash",
    }
    values.update(overrides)
    return VisionConfig.model_validate({"apiKey": values["apiKey"], **values})


def transport(models=None, responses=None) -> httpx.MockTransport:
    models = models if models is not None else [
        {"id": "gemini-3.5-flash"},
    ]
    responses = responses or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            assert "Authorization" in request.headers
            # The key must never appear in the request body or query string.
            assert "sk-secret" not in str(request.url)
            return httpx.Response(200, json={"data": models})
        if request.url.path == "/v1/chat/completions":
            assert "sk-secret" not in str(request.url)
            body = json.loads(request.content) if request.content else {}
            # payload carries image bytes; confirm no key is embedded in it
            assert "sk-secret" not in json.dumps(body)
            return httpx.Response(200, json=responses.get("responses", _ok_response()))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _ok_response(model="gemini-3.5-flash", request_id="req_abc", text="{\"ok\": true}"):
    return {
        "id": request_id,
        "model": model,
        "object": "chat.completion",
        "usage": {"prompt_tokens": 10, "completion_tokens": 42, "total_tokens": 52},
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


def test_config_from_env_resolves_models_and_key() -> None:
    config = VisionConfig.from_env({
        "TUZI_API_KEY": "sk-env-key",
        "TUZI_VISION_PRIMARY_MODEL": "gemini-3.5-flash",
        "TUZI_VISION_REVIEW_MODEL": "gemini-3.5-flash",
    })
    assert config.primaryModel == "gemini-3.5-flash"
    assert config.reviewModel == "gemini-3.5-flash"
    assert config.apiKey.get_secret_value() == "sk-env-key"


def test_config_from_env_reads_key_from_file(tmp_path) -> None:
    key_file = tmp_path / "key.txt"
    key_file.write_text("sk-file-key\n", encoding="utf-8")
    config = VisionConfig.from_env({
        "TUZI_API_KEY_FILE": str(key_file),
        "TUZI_VISION_PRIMARY_MODEL": "gemini-3.5-flash",
        "TUZI_VISION_REVIEW_MODEL": "gemini-3.5-flash",
    })
    assert config.apiKey.get_secret_value() == "sk-file-key"


def test_config_rejects_missing_model() -> None:
    with pytest.raises(ValueError, match="model config"):
        VisionConfig.from_env({"TUZI_API_KEY": "sk-x"})


def test_config_redacted_dict_hides_key() -> None:
    config = make_config()
    dumped = config.redacted_dict()
    assert dumped["apiKey"] == "<redacted>"
    assert "sk-secret" not in json.dumps(dumped)


def test_probe_ok_when_model_present_and_json_parses() -> None:
    config = make_config()
    with httpx.Client(transport=transport()) as client:
        profile = probe_model(config, "gemini-3.5-flash", client)
    assert profile.status is ProbeStatus.OK
    assert profile.imagesSupported is True
    assert profile.jsonSchemaSupported is True
    assert profile.requestIdAvailable is True
    assert profile.actualModel == "gemini-3.5-flash"
    assert "sk-secret" not in json.dumps(profile.redacted_dict())


def test_probe_reports_model_missing() -> None:
    config = make_config()
    # an unrelated model id in the list so the probed one is absent
    with httpx.Client(transport=transport(models=[{"id": "gemini-2.5-flash"}])) as client:
        profile = probe_model(config, "gemini-3.5-flash", client)
    assert profile.status is ProbeStatus.MODEL_NOT_AVAILABLE
    assert profile.imagesSupported is False


def test_probe_reports_structured_output_unsupported_on_garbage() -> None:
    config = make_config()
    transport_ = transport(responses={"responses": _ok_response(text="Sure, here is the JSON:\n```json\n{not json}\n```")})
    with httpx.Client(transport=transport_) as client:
        profile = probe_model(config, "gemini-3.5-flash", client)
    assert profile.status is ProbeStatus.STRUCTURED_OUTPUT_UNSUPPORTED
    assert profile.jsonSchemaSupported is False


def test_probe_reports_image_unsupported_on_http_415() -> None:
    config = make_config()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemini-3.5-flash"}]})
        return httpx.Response(415)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        profile = probe_model(config, "gemini-3.5-flash", client)
    assert profile.status is ProbeStatus.IMAGE_UNSUPPORTED


def test_probe_profile_validates_against_schema_and_is_persistable() -> None:
    from jsonschema import Draft202012Validator, FormatChecker
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/vision-capability-profile.schema.json").read_text(encoding="utf-8")
    )
    profile = VisionCapabilityProfile(
        provider="tuzi", baseUrl="https://api.tu-zi.com", model="gemini-3.5-flash",
        status=ProbeStatus.OK, imagesSupported=True, jsonSchemaSupported=True,
        requestIdAvailable=True, latencyMs=120, actualModel="gemini-3.5-flash",
        maxTokens=42, checkedAt=datetime.now(timezone.utc).isoformat(), detail="probe ok",
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(profile.redacted_dict())
