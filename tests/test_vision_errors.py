"""T05: normalised vision error envelopes.

Every failure maps to a stable code + recovery action, and no error envelope may
carry secret material. This mirrors the raw ``VisionError`` enum into a
caller-actionable ``VisionFailure``.
"""

from __future__ import annotations

import json

import pytest

from pano_platform.vision.errors import (
    RecoveryAction,
    VisionFailure,
    failure_for,
    recoverable_errors,
)
from pano_platform.vision.provider import VisionError


def test_failure_has_stable_code_and_recovery() -> None:
    failure = failure_for(VisionError.VISION_RATE_LIMITED)
    assert failure.code is VisionError.VISION_RATE_LIMITED
    assert failure.retryable is True
    assert failure.recovery is RecoveryAction.RETRY


def test_upstream_error_triggers_cv_fallback() -> None:
    failure = failure_for(VisionError.VISION_UPSTREAM_ERROR)
    assert failure.recovery is RecoveryAction.FALLBACK_TO_CV
    assert failure.retryable is True


def test_model_missing_disables_provider() -> None:
    assert failure_for(VisionError.VISION_MODEL_NOT_AVAILABLE).recovery is RecoveryAction.DISABLE_PROVIDER
    assert failure_for(VisionError.VISION_STRUCTURED_OUTPUT_UNSUPPORTED).recovery is RecoveryAction.DISABLE_PROVIDER


def test_schema_invalid_and_filtered_go_to_review() -> None:
    assert failure_for(VisionError.VISION_SCHEMA_INVALID).recovery is RecoveryAction.REVIEW
    assert failure_for(VisionError.VISION_CONTENT_FILTERED).recovery is RecoveryAction.REVIEW
    assert failure_for(VisionError.VISION_OUTPUT_TRUNCATED).recovery is RecoveryAction.REVIEW


def test_input_unsupported_rejects() -> None:
    assert failure_for(VisionError.VISION_INPUT_UNSUPPORTED).recovery is RecoveryAction.REJECT


def test_failure_serializable_without_secrets() -> None:
    failure = failure_for(VisionError.VISION_TIMEOUT, reason="deadline")
    dumped = json.dumps(failure.to_dict())
    assert "api_key" not in dumped.lower()
    assert "sk-" not in dumped
    assert dumped  # non-empty JSON


def test_recoverable_error_set() -> None:
    assert VisionError.VISION_RATE_LIMITED in recoverable_errors()
    assert VisionError.VISION_TIMEOUT in recoverable_errors()
    assert VisionError.VISION_UPSTREAM_ERROR in recoverable_errors()
    assert VisionError.VISION_SCHEMA_INVALID not in recoverable_errors()


def test_failure_reason_codes_are_preserved() -> None:
    failure = failure_for(VisionError.VISION_SCHEMA_INVALID, reason="missing polygon")
    assert failure.reasonCodes == ("missing polygon",)
    assert failure.to_dict()["reasonCodes"] == ["missing polygon"]
