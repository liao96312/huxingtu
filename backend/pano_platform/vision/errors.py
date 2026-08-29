"""Normalised vision error envelopes.

Every failure in the vision path collapses to a ``VisionFailure`` with a stable
code (see ``provider.VisionError``), a human-readable message, a retryable flag
and an optional reason code. Nothing here may carry an API key, base64 payload,
signed URL or any raw response material — secrets never enter an error envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .provider import VisionError


class RecoveryAction(StrEnum):
    """What the caller should do with a failure."""

    RETRY = "retry"
    FALLBACK_TO_CV = "fallbackToCv"
    REVIEW = "review"
    REJECT = "reject"
    DISABLE_PROVIDER = "disableProvider"


@dataclass(frozen=True)
class VisionFailure:
    """A normalised vision failure with a safe, auditable description."""

    code: VisionError
    message: str
    retryable: bool = False
    recovery: RecoveryAction = RecoveryAction.REVIEW
    reasonCodes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "recovery": self.recovery.value,
            "reasonCodes": list(self.reasonCodes),
        }


def recoverable_errors() -> set[VisionError]:
    return {
        VisionError.VISION_RATE_LIMITED,
        VisionError.VISION_UPSTREAM_ERROR,
        VisionError.VISION_TIMEOUT,
    }


def failure_for(error: VisionError, reason: str | None = None) -> VisionFailure:
    """Map a ``VisionError`` to a ``VisionFailure`` with recovery semantics."""

    retryable = error in recoverable_errors()
    recovery = {
        VisionError.VISION_RATE_LIMITED: RecoveryAction.RETRY,
        VisionError.VISION_TIMEOUT: RecoveryAction.RETRY,
        VisionError.VISION_UPSTREAM_ERROR: RecoveryAction.FALLBACK_TO_CV,
        VisionError.VISION_MODEL_NOT_AVAILABLE: RecoveryAction.DISABLE_PROVIDER,
        VisionError.VISION_STRUCTURED_OUTPUT_UNSUPPORTED: RecoveryAction.DISABLE_PROVIDER,
        VisionError.VISION_PROVIDER_UNAVAILABLE: RecoveryAction.DISABLE_PROVIDER,
        VisionError.VISION_INPUT_UNSUPPORTED: RecoveryAction.REJECT,
        VisionError.VISION_CONTENT_FILTERED: RecoveryAction.REVIEW,
        VisionError.VISION_OUTPUT_TRUNCATED: RecoveryAction.REVIEW,
        VisionError.VISION_SCHEMA_INVALID: RecoveryAction.REVIEW,
        # fallback for any new code
    }.get(error, RecoveryAction.REVIEW)

    defaults = {
        VisionError.VISION_RATE_LIMITED: "rate limited by provider",
        VisionError.VISION_TIMEOUT: "provider timed out",
        VisionError.VISION_UPSTREAM_ERROR: "provider upstream error",
        VisionError.VISION_MODEL_NOT_AVAILABLE: "model not available on provider",
        VisionError.VISION_STRUCTURED_OUTPUT_UNSUPPORTED: "structured output unsupported",
        VisionError.VISION_PROVIDER_UNAVAILABLE: "provider unavailable",
        VisionError.VISION_INPUT_UNSUPPORTED: "input unsupported",
        VisionError.VISION_CONTENT_FILTERED: "content filtered",
        VisionError.VISION_OUTPUT_TRUNCATED: "provider output truncated",
        VisionError.VISION_SCHEMA_INVALID: "provider output failed schema validation",
    }
    codes = tuple([reason] if reason else [])
    return VisionFailure(
        code=error,
        message=defaults.get(error, "vision error"),
        retryable=retryable,
        recovery=recovery,
        reasonCodes=codes,
    )
