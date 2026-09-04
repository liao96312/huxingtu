"""Vision provider abstraction: protocol, errors, run result and shared helpers.

The business layer depends only on ``VisionProvider``. It never sees Tuzi-,
Gemini- or provider-specific fields. All request construction, response parsing
and retry logic lives behind here so the concrete adapter stays thin.

Shared helpers that the capability probe and the concrete adapter both need live
in this module so there is exactly one place that builds the ``/v1/responses``
payload and one place that extracts structured JSON from a raw response.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from .config import VisionConfig
from .contracts import (
    VisionParseRequest,
    VisionReviewRequest,
    VlmDraft,
)

DEFAULT_MAX_TOKENS = 16384


class VisionError(StrEnum):
    """Normalised provider error codes (mirrors the docs plan)."""

    VISION_PROVIDER_UNAVAILABLE = "VISION_PROVIDER_UNAVAILABLE"
    VISION_MODEL_NOT_AVAILABLE = "VISION_MODEL_NOT_AVAILABLE"
    VISION_INPUT_UNSUPPORTED = "VISION_INPUT_UNSUPPORTED"
    VISION_STRUCTURED_OUTPUT_UNSUPPORTED = "VISION_STRUCTURED_OUTPUT_UNSUPPORTED"
    VISION_SCHEMA_INVALID = "VISION_SCHEMA_INVALID"
    VISION_OUTPUT_TRUNCATED = "VISION_OUTPUT_TRUNCATED"
    VISION_CONTENT_FILTERED = "VISION_CONTENT_FILTERED"
    VISION_RATE_LIMITED = "VISION_RATE_LIMITED"
    VISION_TIMEOUT = "VISION_TIMEOUT"
    VISION_UPSTREAM_ERROR = "VISION_UPSTREAM_ERROR"


@dataclass(frozen=True)
class VisionRunResult:
    """What a provider call returns to the business layer."""

    draft: VlmDraft | None
    evidence: dict
    error: VisionError | None = None


class VisionProvider(Protocol):
    def capabilities(self) -> dict: ...

    def parse(self, request: VisionParseRequest, image_bytes: bytes) -> VisionRunResult: ...

    def review(self, request: VisionReviewRequest, image_bytes: bytes) -> VisionRunResult: ...


@dataclass(frozen=True)
class PreparedPayload:
    """A ready-to-send request body plus its digest for evidence."""

    body: dict
    bodySha256: str


def build_parse_payload(
    config: VisionConfig,
    model: str,
    prompt: str,
    draft_schema: dict,
    image_bytes: bytes,
    schema_name: str = "floorplan_draft_v1",
) -> PreparedPayload:
    """Build a ``/v1/chat/completions`` payload with strict JSON Schema output.

    The Tuzi gateway does not forward ``/v1/responses`` (``convert_request_failed``),
    so structured extraction runs on the OpenAI-compatible chat endpoint using
    ``response_format.type=json_schema`` and ``finish_reason=stop``.
    """

    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": to_data_uri(image_bytes)}},
                ],
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": draft_schema},
        },
        "max_tokens": DEFAULT_MAX_TOKENS,
    }
    return PreparedPayload(body=body, bodySha256=canonical_hash(body))


def auth_headers(config: VisionConfig) -> dict:
    return {"Authorization": f"Bearer {config.apiKey.get_secret_value()}"}


def to_data_uri(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    content_type = (
        "image/jpeg" if image_bytes.startswith(b"\xff\xd8")
        else "image/webp" if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP"
        else "image/png"
    )
    return f"data:{content_type};base64,{encoded}"


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def extract_output_text(response: dict) -> str | None:
    response = unwrap_gateway_response(response)
    """Pull the assistant message ``content`` from a chat.completion payload."""

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text")
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                return block.get("text")
    return None


def extract_finish_reason(response: dict) -> str | None:
    response = unwrap_gateway_response(response)
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    value = choices[0].get("finish_reason")
    return value if isinstance(value, str) else None


def parse_draft(text: str) -> VlmDraft:
    """Parse ``text`` as strict JSON into a ``VlmDraft``.

    The provider is asked to return raw JSON; if it still wraps the payload in a
    markdown code fence we strip the fence before parsing (the gateway sometimes
    emits a fence even under ``finish_reason=stop``). Anything that is still not
    JSON is a schema/parse failure.
    """

    cleaned = _strip_json_fence(text)
    try:
        value = json.loads(cleaned)
    except ValueError as error:
        raise ValueError("provider returned non-JSON output") from error
    if not isinstance(value, dict):
        raise ValueError("provider JSON is not an object")
    return VlmDraft.model_validate(value)


def _strip_json_fence(text: str) -> str:
    """Remove a surrounding ```json ... ``` fence if present."""

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().lower().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def normalize_http_error(status: int, model: str) -> VisionError:
    """Map an HTTP status to a normalised vision error code."""

    if status in (401, 403):
        return VisionError.VISION_PROVIDER_UNAVAILABLE
    if status == 404:
        return VisionError.VISION_MODEL_NOT_AVAILABLE
    if status in (400, 415, 422):
        return VisionError.VISION_INPUT_UNSUPPORTED
    if status == 429:
        return VisionError.VISION_RATE_LIMITED
    if status in (502, 503, 504):
        return VisionError.VISION_UPSTREAM_ERROR
    return VisionError.VISION_UPSTREAM_ERROR


def is_retryable(error: VisionError) -> bool:
    """Safely retryable transient failures.

    Auth/config (401/403) and input unsupported (400/415/422) are NOT retried;
    only rate-limit, upstream 5xx and timeouts are. Transport-level connect
    failures are retried by their own loop in the adapter, not here.
    """

    return error in (
        VisionError.VISION_RATE_LIMITED,
        VisionError.VISION_UPSTREAM_ERROR,
        VisionError.VISION_TIMEOUT,
    )


def extract_request_id(response: httpx.Response) -> str | None:
    request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
    if request_id:
        return request_id
    data = _safe_json(response)
    if isinstance(data, dict):
        nested = data.get("data")
        return data.get("request_id") or data.get("id") or (
            nested.get("id") if isinstance(nested, dict) else None
        )
    return None


def extract_usage(response: httpx.Response) -> dict:
    data = _safe_json(response)
    if isinstance(data, dict):
        normalized = unwrap_gateway_response(data)
        if isinstance(normalized.get("usage"), dict):
            return normalized["usage"]
    return {}


def unwrap_gateway_response(response: dict) -> dict:
    """Unwrap the documented Tuzi code/message/request_id/data envelope."""

    nested = response.get("data")
    if isinstance(nested, dict) and any(key in nested for key in ("choices", "output", "usage")):
        return nested
    return response


def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None
