"""Auditable vision run evidence with canonical hashing.

Every provider call produces a ``VisionRunEvidence`` record that is keyed by the
full job key (image + provider + base URL profile + model + prompt version +
draft schema version + preprocessing version). The raw response is reduced to its
SHA-256 so nothing sensitive — no key, no base64, no signed URL — ever gets
persisted; the requirement is traceability, not raw capture.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .contracts import VlmDraft
from .provider import canonical_hash


def normalized_draft_hash(draft: VlmDraft | None) -> str | None:
    """Canonical SHA-256 of a draft's ordered field dump, or None if absent."""

    if draft is None:
        return None
    return canonical_hash(draft.model_dump())


def raw_response_hash(raw_response: dict | None) -> str | None:
    """Canonical SHA-256 of the provider raw response JSON, or None."""

    if raw_response is None:
        return None
    return canonical_hash(raw_response)


def build_evidence(
    *,
    provider: str,
    endpoint: str,
    requested_model: str,
    actual_model: str | None,
    provider_request_id: str | None,
    prompt_version: str,
    draft_schema_version: str,
    image_sha256: str,
    latency_ms: int | None,
    usage: dict | None,
    attempt_count: int,
    raw_response: dict | None,
    draft: VlmDraft | None,
    status: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    job_key: str | None = None,
) -> dict:
    """Build a full auditable evidence dict with canonical hashes."""

    record: dict[str, Any] = {
        "provider": provider,
        "endpoint": endpoint,
        "requestedModel": requested_model,
        "actualModel": actual_model,
        "providerRequestId": provider_request_id,
        "promptVersion": prompt_version,
        "draftSchemaVersion": draft_schema_version,
        "imageSha256": image_sha256,
        "latencyMs": latency_ms,
        "usage": usage or {},
        "attemptCount": attempt_count,
        "rawResponseSha256": raw_response_hash(raw_response),
        "normalizedDraftSha256": normalized_draft_hash(draft),
        "status": status or ("candidate" if draft is not None else "rejected"),
        "errorCode": error_code,
        "errorDetail": error_detail,
        "recordedAt": datetime.now().astimezone().isoformat(),
    }
    if job_key:
        record["jobKey"] = job_key
    return record


def redact_evidence(evidence: dict) -> dict:
    """Return a copy of evidence safe for logs (strip any potential secrets)."""

    safe_keys = [
        "provider", "endpoint", "requestedModel", "actualModel", "providerRequestId",
        "promptVersion", "draftSchemaVersion", "imageSha256", "latencyMs", "usage",
        "attemptCount", "rawResponseSha256", "normalizedDraftSha256", "status",
        "errorCode", "errorDetail", "recordedAt", "jobKey",
    ]
    return {key: evidence.get(key) for key in safe_keys if key in evidence}
