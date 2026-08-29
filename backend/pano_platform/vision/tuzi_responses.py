"""Tuzi OpenAI-Responses-compatible vision adapter.

Thin implementation of ``VisionProvider`` on top of the shared request/parse
helpers in ``provider.py``. It performs bounded retries on transient failures,
normalises errors to the ``VisionError`` enum, records evidence (request id,
actual model, usage, latency, hashes) and never guesses JSON from Markdown.

The adapter publishes a ``VlmDraft`` *candidate* only. Geometry gates in the
fusion layer decide whether anything in the draft may become a fact artifact.
"""

from __future__ import annotations

import time
from typing import Iterable

import httpx

from .config import VisionConfig
from .contracts import (
    VisionParseRequest,
    VisionReviewRequest,
    VlmDraft,
    vlm_draft_openai_schema,
    vlm_draft_schema,
)
from .evidence import build_evidence
from .prompts import PARSE_PROMPT_ID, REVIEW_PROMPT_ID, parse_prompt, review_prompt
from .provider import (
    VisionError,
    VisionProvider,
    VisionRunResult,
    auth_headers,
    build_parse_payload,
    extract_request_id,
    extract_usage,
    extract_output_text,
    is_retryable,
    normalize_http_error,
    parse_draft,
)

MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {429, 502, 503, 504}

DEFAULT_PROMPT = (
    "You are parsing an orthogonal floorplan into a strict JSON draft. "
    "All coordinates must be NORMALISED to [0,1] relative to the image: "
    "nx = pixel_x / image_width, ny = pixel_y / image_height, origin top-left, y DOWN. "
    "Polygons must be closed (>=3 points), no self-intersection. "
    "Return ONLY the JSON object matching the schema."
)


class TuziResponsesVisionProvider(VisionProvider):
    """OpenAI-compatible Tuzi adapter over /v1/chat/completions; no geometry publication.

    The Tuzi gateway does not forward OpenAI ``/v1/responses`` (it returns
    ``convert_request_failed``), so structured extraction runs on the chat
    completions endpoint with ``response_format.json_schema``.
    """

    endpoint = "/v1/chat/completions"

    def __init__(self, config: VisionConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=config.timeoutSeconds)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def capabilities(self) -> dict:
        return self._config.redacted_dict()

    def parse(self, request: VisionParseRequest, image_bytes: bytes) -> VisionRunResult:
        prompt = parse_prompt()
        return self._run(
            model=request.model,
            prompt_version=request.promptVersion or PARSE_PROMPT_ID,
            draft_schema_version=request.draftSchemaVersion,
            image_sha256=request.imageSha256,
            image_bytes=image_bytes,
            schema_name="floorplan_draft_v1",
            prompt=prompt,
        )

    def review(self, request: VisionReviewRequest, image_bytes: bytes) -> VisionRunResult:
        """Re-classify only the given candidates; never re-parse the whole image."""

        prompt = review_prompt()
        return self._run(
            model=request.model,
            prompt_version=request.promptVersion or REVIEW_PROMPT_ID,
            draft_schema_version=request.draftSchemaVersion,
            image_sha256=request.imageSha256,
            image_bytes=image_bytes,
            schema_name="floorplan_review_v1",
            prompt=prompt,
        )

    def _run(
        self,
        model: str,
        prompt_version: str,
        draft_schema_version: str,
        image_sha256: str,
        image_bytes: bytes,
        schema_name: str,
        prompt: str = DEFAULT_PROMPT,
        draft_schema: dict | None = None,
    ) -> VisionRunResult:
        from .contracts import SCHEMA_VERSION

        schema = draft_schema or vlm_draft_openai_schema()
        last_error: VisionError = VisionError.VISION_UPSTREAM_ERROR
        attempt = 0
        last_evidence: dict = {}
        corrective_prompt = prompt

        while attempt < MAX_ATTEMPTS:
            attempt += 1
            started = time.perf_counter()
            try:
                prepared = build_parse_payload(
                    self._config, model, corrective_prompt, schema, image_bytes, schema_name=schema_name
                )
                response = self._client.post(
                    f"{self._config.baseUrl}{self.endpoint}",
                    headers=auth_headers(self._config),
                    json=prepared.body,
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.TimeoutException as error:
                last_error = VisionError.VISION_TIMEOUT
                last_evidence = self._evidence(attempt, started, image_sha256, prompt_version, draft_schema_version, model, error=last_error)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                continue
            except httpx.HTTPStatusError as error:
                last_error = normalize_http_error(error.response.status_code, model)
                evidence = self._evidence(
                    attempt, started, image_sha256, prompt_version, draft_schema_version, model,
                    error=last_error, request_id=extract_request_id(error.response),
                )
                # Do not let a follow-up gateway 400 hide a useful validation
                # error from the provider's previous structured response.
                if last_evidence.get("errorCode") == VisionError.VISION_SCHEMA_INVALID.value:
                    return VisionRunResult(
                        draft=None,
                        evidence=last_evidence,
                        error=VisionError.VISION_SCHEMA_INVALID,
                    )
                # Tuzi intermittently returns HTTP 400 for the same validated
                # image/schema request that succeeds moments later. Retry this
                # gateway-specific ambiguity, but preserve INPUT_UNSUPPORTED if
                # all bounded attempts fail.
                if (
                    error.response.status_code == 400
                    and last_error is VisionError.VISION_INPUT_UNSUPPORTED
                    and attempt < MAX_ATTEMPTS
                ):
                    last_evidence = evidence
                    time.sleep(2.0 * attempt)
                    continue
                if is_retryable(last_error) and attempt < MAX_ATTEMPTS:
                    last_evidence = evidence
                    time.sleep(self._backoff(attempt))
                    continue
                return VisionRunResult(draft=None, evidence=evidence, error=last_error)
            except (httpx.TransportError) as error:
                last_error = VisionError.VISION_PROVIDER_UNAVAILABLE
                last_evidence = self._evidence(attempt, started, image_sha256, prompt_version, draft_schema_version, model, error=last_error)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(self._backoff(attempt))
                continue

            output_text = extract_output_text(payload)
            if output_text is None:
                last_error = VisionError.VISION_STRUCTURED_OUTPUT_UNSUPPORTED
                last_evidence = self._evidence(attempt, started, image_sha256, prompt_version, draft_schema_version, model, error=last_error, raw=payload)
                if attempt < MAX_ATTEMPTS:
                    continue
                return VisionRunResult(draft=None, evidence=last_evidence, error=last_error)

            try:
                draft = parse_draft(output_text)
            except ValueError as error:
                last_error = VisionError.VISION_SCHEMA_INVALID
                detail = self._validation_detail(error)
                last_evidence = self._evidence(
                    attempt, started, image_sha256, prompt_version,
                    draft_schema_version, model, error=last_error, raw=payload,
                    output_text=output_text, error_detail=detail,
                )
                if attempt < MAX_ATTEMPTS:
                    corrective_prompt = (
                        prompt
                        + "\n\nYour previous JSON failed local validation. Correct it and return the complete JSON object. "
                        + detail[:800]
                    )
                    time.sleep(self._backoff(attempt))
                    continue
                return VisionRunResult(draft=None, evidence=last_evidence, error=last_error)

            evidence = self._evidence(
                attempt, started, image_sha256, prompt_version, draft_schema_version, model,
                request_id=extract_request_id(response),
                usage=extract_usage(response),
                actual_model=payload.get("model"),
                raw=payload,
                normalized_draft=draft,
            )
            return VisionRunResult(draft=draft, evidence=evidence)

        return VisionRunResult(draft=None, evidence=last_evidence, error=last_error)

    def _evidence(
        self,
        attempt: int,
        started: float,
        image_sha256: str,
        prompt_version: str,
        draft_schema_version: str,
        requested_model: str,
        error: VisionError | None = None,
        request_id: str | None = None,
        usage: dict | None = None,
        actual_model: str | None = None,
        raw: dict | None = None,
        output_text: str | None = None,
        normalized_draft: VlmDraft | None = None,
        error_detail: str | None = None,
    ) -> dict:
        latency_ms = int((time.perf_counter() - started) * 1000)
        status = "candidate" if normalized_draft is not None else ("review" if error else "rejected")
        return build_evidence(
            provider="tuzi",
            endpoint=self.endpoint,
            requested_model=requested_model,
            actual_model=actual_model,
            provider_request_id=request_id,
            prompt_version=prompt_version,
            draft_schema_version=draft_schema_version,
            image_sha256=image_sha256,
            latency_ms=latency_ms,
            usage=usage,
            attempt_count=attempt,
            raw_response=raw,
            draft=normalized_draft,
            status=status,
            error_code=error.value if error else None,
            error_detail=error_detail,
        )

    @staticmethod
    def _validation_detail(error: ValueError) -> str:
        """Keep validation locations/messages without persisting model output."""

        import json

        if hasattr(error, "errors"):
            items = [
                {
                    "loc": list(item.get("loc", ())),
                    "type": item.get("type"),
                    "msg": item.get("msg"),
                }
                for item in error.errors()
            ]
            return json.dumps(items, ensure_ascii=False)[:2048]
        return str(error)[:2048]

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0, 0.5 * (2 ** (attempt - 1)))

