"""Tuzi capability discovery and runtime probing.

At startup (or whenever a model configuration changes) we probe each candidate
model once with a tiny generated image and a strict ``json_schema`` request. The
probe decides whether the vision path is usable for that model and records the
observations (actual model id, request id, latency, usage) for audit. A model
that fails the probe is not placed on the vision path; the service may still run
on the pure-CV fallback with the vision track reported ``UNAVAILABLE``.

The probe must never log the API key, image bytes, base64 payloads or signed
URLs; only hashes, ids, timings and counts are recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Mapping

import httpx

from .config import VisionConfig
from .provider import (
    VisionError,
    auth_headers,
    build_parse_payload,
    extract_output_text,
)


class ProbeStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    MODEL_NOT_AVAILABLE = "modelNotAvailable"
    IMAGE_UNSUPPORTED = "imageUnsupported"
    STRUCTURED_OUTPUT_UNSUPPORTED = "structuredOutputUnsupported"


@dataclass(frozen=True)
class VisionCapabilityProfile:
    """Auditable result of probing one provider/model pair."""

    provider: str
    baseUrl: str
    model: str
    status: ProbeStatus
    imagesSupported: bool
    jsonSchemaSupported: bool
    requestIdAvailable: bool
    latencyMs: int | None
    actualModel: str | None
    maxTokens: int | None
    checkedAt: str
    detail: str | None = None

    def redacted_dict(self) -> dict:
        """Serializable dict without any key or payload material."""

        return {
            "provider": self.provider,
            "baseUrl": self.baseUrl,
            "model": self.model,
            "status": self.status.value,
            "imagesSupported": self.imagesSupported,
            "jsonSchemaSupported": self.jsonSchemaSupported,
            "requestIdAvailable": self.requestIdAvailable,
            "latencyMs": self.latencyMs,
            "actualModel": self.actualModel,
            "maxTokens": self.maxTokens,
            "checkedAt": self.checkedAt,
            "detail": self.detail,
        }


# A minimal strict schema shared by the probe. The probe only needs to know
# whether the provider honours strict JSON; the real draft schema is larger.
PROBE_SCHEMA: dict = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

PROBE_IMAGE_BYTES: bytes = b""  # filled lazily by _make_probe_image()


def probe_model(config: VisionConfig, model: str, client: httpx.Client | None = None) -> VisionCapabilityProfile:
    """Probe a single model for image input and strict JSON Schema support."""

    base = _now()
    owns_client = client is None
    client = client or httpx.Client(timeout=config.timeoutSeconds)

    try:
        available = _model_present(client, config, model)
        if not available:
            return VisionCapabilityProfile(
                provider="tuzi", baseUrl=config.baseUrl, model=model,
                status=ProbeStatus.MODEL_NOT_AVAILABLE, imagesSupported=False,
                jsonSchemaSupported=False, requestIdAvailable=False, latencyMs=None,
                actualModel=None, maxTokens=None, checkedAt=base,
                detail="model missing from /v1/models",
            )

        start = datetime.now(timezone.utc)
        response = _run_probe(client, config, model)
        latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

        actual = response.get("model") or model
        request_id = response.get("id")
        usage = response.get("usage") or {}
        max_tokens = usage.get("completion_tokens") or usage.get("output_tokens") if isinstance(usage, dict) else None
        output = extract_output_text(response)
        parsed = _try_json(output) if output else None

        if parsed is None:
            return VisionCapabilityProfile(
                provider="tuzi", baseUrl=config.baseUrl, model=model,
                status=ProbeStatus.STRUCTURED_OUTPUT_UNSUPPORTED, imagesSupported=True,
                jsonSchemaSupported=False, requestIdAvailable=bool(request_id),
                latencyMs=latency, actualModel=actual, maxTokens=max_tokens, checkedAt=base,
                detail="returned output did not parse as strict JSON",
            )

        return VisionCapabilityProfile(
            provider="tuzi", baseUrl=config.baseUrl, model=model,
            status=ProbeStatus.OK, imagesSupported=True, jsonSchemaSupported=True,
            requestIdAvailable=bool(request_id), latencyMs=latency, actualModel=actual,
            maxTokens=max_tokens, checkedAt=base, detail="probe ok",
        )
    except httpx.HTTPStatusError as error:
        return VisionCapabilityProfile(
            provider="tuzi", baseUrl=config.baseUrl, model=model,
            status=_status_from_http(error.response.status_code), imagesSupported=False,
            jsonSchemaSupported=False, requestIdAvailable=False, latencyMs=None,
            actualModel=None, maxTokens=None, checkedAt=base,
            detail=f"http {error.response.status_code}",
        )
    except (httpx.TimeoutException, httpx.TransportError) as error:
        return VisionCapabilityProfile(
            provider="tuzi", baseUrl=config.baseUrl, model=model,
            status=ProbeStatus.UNAVAILABLE, imagesSupported=False,
            jsonSchemaSupported=False, requestIdAvailable=False, latencyMs=None,
            actualModel=None, maxTokens=None, checkedAt=base,
            detail=type(error).__name__,
        )
    finally:
        if owns_client:
            client.close()


def probe_primary_and_review(config: VisionConfig) -> tuple[VisionCapabilityProfile, VisionCapabilityProfile]:
    """Probe both configured models independently."""

    with httpx.Client(timeout=config.timeoutSeconds) as client:
        return (
            probe_model(config, config.primaryModel, client),
            probe_model(config, config.reviewModel, client),
        )


def _model_present(client: httpx.Client, config: VisionConfig, model: str) -> bool:
    response = client.get(f"{config.baseUrl}/v1/models", headers=auth_headers(config))
    response.raise_for_status()
    data = response.json()
    models = data.get("data", data.get("models", []))
    ids = [item.get("id") for item in models if isinstance(item, dict)]
    return model in ids


def _run_probe(client: httpx.Client, config: VisionConfig, model: str) -> dict:
    payload = build_parse_payload(
        config,
        model,
        "Return exactly this JSON object, no other text: {\"ok\": true}",
        PROBE_SCHEMA,
        _make_probe_image(),
        schema_name="probe_draft",
    ).body
    response = client.post(
        f"{config.baseUrl}/v1/chat/completions",
        headers=auth_headers(config),
        json=payload,
    )
    response.raise_for_status()
    return response.json()


def _make_probe_image() -> bytes:
    if PROBE_IMAGE_BYTES:
        return PROBE_IMAGE_BYTES
    import cv2
    import numpy as np

    cell = np.full((8, 8, 3), 255, np.uint8)
    cell[0:4, 0:4] = 0
    image = np.tile(cell, (8, 8, 1))
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("failed to encode probe image")
    return encoded.tobytes()


def _try_json(text: str) -> dict | None:
    import json

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().lower().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        value = json.loads(stripped)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _status_from_http(status: int) -> ProbeStatus:
    if status == 404:
        return ProbeStatus.MODEL_NOT_AVAILABLE
    if status in (400, 415, 422):
        return ProbeStatus.IMAGE_UNSUPPORTED
    return ProbeStatus.UNAVAILABLE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
