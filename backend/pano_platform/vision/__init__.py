"""Vision provider package (Tuzi / OpenAI compatible).

The vision path is deliberately decoupled from geometry: it produces candidate
``VlmDraft`` artifacts only, and never publishes a wall, room or opening into a
fact artifact without passing the geometry fusion gates. See
``docs/plan/floorplan-recognition-v2.md``.
"""

from .capabilities import (
    ProbeStatus,
    VisionCapabilityProfile,
    probe_model,
    probe_primary_and_review,
)
from .cache import CachedVisionResult, VisionJobKey, VisionResultCache
from .errors import RecoveryAction, VisionFailure, failure_for
from .config import VisionConfig
from .input_artifact import (
    PREPROCESSING_VERSION,
    VisionInputArtifact,
    VisionInputError,
    detect_content_type,
    image_dimensions,
    prepare_vision_input,
)
from .prompts import PARSE_PROMPT_ID, REVIEW_PROMPT_ID, parse_prompt, review_prompt
from .contracts import (
    DraftConfidence,
    DraftStatus,
    NormPoint,
    OpeningDraft,
    OpeningDraftType,
    RoomDraft,
    RoomDraftType,
    ScaleCandidate,
    VisionParseRequest,
    VisionReviewRequest,
    VisionRunEvidence,
    VlmDraft,
    vlm_draft_openai_schema,
    vlm_draft_schema,
)

__all__ = [
    "DraftConfidence",
    "DraftStatus",
    "NormPoint",
    "OpeningDraft",
    "OpeningDraftType",
    "ProbeStatus",
    "RoomDraft",
    "RoomDraftType",
    "ScaleCandidate",
    "VisionCapabilityProfile",
    "VisionConfig",
    "VisionParseRequest",
    "VisionReviewRequest",
    "VisionRunEvidence",
    "VlmDraft",
    "probe_model",
    "probe_primary_and_review",
    "vlm_draft_openai_schema",
    "vlm_draft_schema",
    "PARSE_PROMPT_ID",
    "PREPROCESSING_VERSION",
    "REVIEW_PROMPT_ID",
    "VisionInputArtifact",
    "VisionInputError",
    "detect_content_type",
    "image_dimensions",
    "parse_prompt",
    "prepare_vision_input",
    "review_prompt",
    "CachedVisionResult",
    "RecoveryAction",
    "VisionFailure",
    "VisionJobKey",
    "VisionResultCache",
    "failure_for",
]
