"""Frozen VLM input/output contracts for the vision track.

Coordinates are normalised to the source image: ``[0, 1]`` with the origin at
the top-left and ``y`` increasing downward. A ``VlmDraft`` is a *candidate*
artifact produced by the vision provider; no wall, room or opening in it may be
published into a fact artifact until it has passed the geometry fusion gates.

Strictness (all enforced here, and mirrored by ``schemas/*.schema.json``):

* coordinates inside ``[0, 1]`` and finite (no NaN / infinities);
* no extra fields (``extra="forbid"``);
* bounded cardinality (rooms <= 64, openings <= 256, scale candidates <= 32);
* room polygons closed, non-degenerate, non-self-intersecting, 3..64 points;
* enumerated types only (no free-text room/opening labels);
* a draft may additionally carry ``warnings``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, RootModel, field_validator, model_validator

from ..contracts import StrictModel

MAX_ROOMS = 64
MAX_OPENINGS = 256
MAX_SCALE_CANDIDATES = 32
MAX_ROOM_POINTS = 64

SCHEMA_VERSION = "0.1.0"


class NormPoint(RootModel):
    """Normalised coordinate as ``[x, y]`` with both components in ``[0, 1]``."""

    root: tuple[float, float]

    @field_validator("root")
    @classmethod
    def validate_range(cls, value: tuple[float, float]) -> tuple[float, float]:
        for component in value:
            if not (-1e-9 <= component <= 1 + 1e-9):
                raise ValueError("normalised coordinate must be within [0, 1]")
        # settle tiny floating drift back inside the unit square
        return (min(1.0, max(0.0, value[0])), min(1.0, max(0.0, value[1])))

    @property
    def x(self) -> float:
        return self.root[0]

    @property
    def y(self) -> float:
        return self.root[1]


class DraftStatus(StrEnum):
    CANDIDATE = "candidate"
    REVIEW = "review"
    REJECTED = "rejected"


class RoomDraftType(StrEnum):
    # Aligned with docs/plan/annotation-spec-v1.md (livingDining was added).
    BEDROOM = "bedroom"
    LIVING = "living"
    DINING = "dining"
    LIVING_DINING = "livingDining"
    KITCHEN = "kitchen"
    BATHROOM = "bathroom"
    BALCONY = "balcony"
    STUDY = "study"
    OTHER = "other"


class OpeningDraftType(StrEnum):
    DOOR = "door"
    WINDOW = "window"
    DOORWAY = "doorway"


class DraftConfidence(StrEnum):
    NONE = "none"
    DOOR_ARC = "doorArc"
    DOOR_LEAF = "doorLeaf"
    PARALLEL_LINES = "parallelLines"


class ScaleCandidate(StrictModel):
    lengthMm: float = Field(gt=0)
    pixelStart: NormPoint
    pixelEnd: NormPoint
    text: str = Field(default="", max_length=64)
    # Provider confidence is advisory only and never a publication gate. Some
    # Tuzi upstream models omit it despite json_schema, so omission means zero.
    confidence: float = Field(default=0.0, ge=0, le=1)


class RoomDraft(StrictModel):
    type: RoomDraftType
    name: str | None = Field(default=None, max_length=64)
    areaLabelM2: float | None = Field(default=None, gt=0)
    polygon: list[NormPoint] = Field(min_length=3, max_length=MAX_ROOM_POINTS)
    confidence: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_polygon(self) -> RoomDraft:
        if self.polygon[0].x == self.polygon[-1].x and self.polygon[0].y == self.polygon[-1].y:
            raise ValueError("room polygon must not repeat its first point")
        area = _signed_area(self.polygon)
        if abs(area) < 1e-9:
            raise ValueError("room polygon is degenerate (zero area)")
        if not _is_simple(self.polygon):
            raise ValueError("room polygon self-intersects")
        return self


class OpeningDraft(StrictModel):
    type: OpeningDraftType
    evidence: list[DraftConfidence] = Field(default_factory=list)
    pixelStart: NormPoint
    pixelEnd: NormPoint
    confidence: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_extent(self) -> OpeningDraft:
        if self.pixelStart.x == self.pixelEnd.x and self.pixelStart.y == self.pixelEnd.y:
            raise ValueError("opening endpoints must differ")
        return self


class VlmDraft(StrictModel):
    scaleCandidates: list[ScaleCandidate] = Field(default_factory=list, max_length=MAX_SCALE_CANDIDATES)
    rooms: list[RoomDraft] = Field(default_factory=list, max_length=MAX_ROOMS)
    openings: list[OpeningDraft] = Field(default_factory=list, max_length=MAX_OPENINGS)
    warnings: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_has_any_content(self) -> VlmDraft:
        # An empty draft is valid only if it explains itself (e.g. "not a
        # floorplan"); otherwise it is an unusable response.
        if not self.rooms and not self.openings and not self.scaleCandidates:
            if not self.warnings:
                raise ValueError("draft must contain at least one room, opening or scale candidate, or a warning")
        return self


class VisionParseRequest(StrictModel):
    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    imageSha256: str
    provider: str = "tuzi"
    model: str
    promptVersion: str = Field(min_length=1, max_length=64)
    draftSchemaVersion: str = Field(min_length=1, max_length=64)
    sourceWidth: int = Field(gt=0)
    sourceHeight: int = Field(gt=0)


class VisionReviewRequest(StrictModel):
    """A review re-asks the provider to classify only the given candidates."""

    schemaVersion: Literal["0.1.0"] = SCHEMA_VERSION
    imageSha256: str
    provider: str = "tuzi"
    model: str
    promptVersion: str = Field(min_length=1, max_length=64)
    draftSchemaVersion: str = Field(min_length=1, max_length=64)
    candidates: VlmDraft


class VisionRunEvidence(StrictModel):
    provider: str = "tuzi"
    endpoint: str = "/v1/chat/completions"
    requestedModel: str
    actualModel: str | None = None
    providerRequestId: str | None = None
    promptVersion: str = Field(min_length=1, max_length=64)
    draftSchemaVersion: str = Field(min_length=1, max_length=64)
    imageSha256: str
    latencyMs: int | None = Field(default=None, ge=0)
    usage: dict = Field(default_factory=dict)
    attemptCount: int = Field(default=1, ge=1)
    rawResponseSha256: str | None = None
    normalizedDraftSha256: str | None = None
    status: DraftStatus = DraftStatus.CANDIDATE
    errorCode: str | None = Field(default=None, max_length=64)
    errorDetail: str | None = Field(default=None, max_length=2048)
    recordedAt: str = Field(default_factory=lambda: datetime.now().astimezone().isoformat())


def vlm_draft_schema() -> dict:
    """The strict JSON Schema matching ``VlmDraft``, used for validation.

    Loaded from the checked-in schema (single source of truth with the fixture).
    This is NOT what is sent to the provider -- see ``vlm_draft_openai_schema``.
    """

    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "schemas" / "vlm-draft.schema.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("schemas/vlm-draft.schema.json is required")


def vlm_draft_openai_schema() -> dict:
    """A flattened OpenAPI-subset schema accepted by the Gemini ``response_schema``.

    The Gemini/OpenAI structured-output path rejects JSON-Schema meta keys
    (``$id``/``$defs``/``$ref``), so ``$ref`` references are inlined and the
    dialect-2020-12 wrapper is stripped. This is the schema sent to the provider;
    the full ``vlm_draft_schema`` still validates the parsed draft.
    """

    norm_point = {
        "type": "array",
        "items": {"type": "number", "minimum": 0, "maximum": 1},
        "minItems": 2,
        "maxItems": 2,
    }
    return {
        "type": "object",
        "properties": {
            "scaleCandidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "lengthMm": {"type": "number"},
                        "pixelStart": norm_point,
                        "pixelEnd": norm_point,
                        "text": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["lengthMm", "pixelStart", "pixelEnd"],
                    "additionalProperties": False,
                },
            },
            "rooms": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["bedroom", "living", "dining", "livingDining", "kitchen", "bathroom", "balcony", "study", "other"]},
                        "name": {"type": "string"},
                        "areaLabelM2": {"type": "number"},
                        "polygon": {"type": "array", "items": norm_point, "minItems": 3},
                        "confidence": {"type": "number"},
                    },
                    "required": ["type", "polygon"],
                    "additionalProperties": False,
                },
            },
            "openings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["door", "window", "doorway"]},
                        "evidence": {"type": "array", "items": {"type": "string", "enum": ["none", "doorArc", "doorLeaf", "parallelLines"]}},
                        "pixelStart": norm_point,
                        "pixelEnd": norm_point,
                        "confidence": {"type": "number"},
                    },
                    "required": ["type", "pixelStart", "pixelEnd"],
                    "additionalProperties": False,
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["scaleCandidates", "rooms", "openings", "warnings"],
        "additionalProperties": False,
    }


def _signed_area(points) -> float:
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index in range(len(points)):
        current = points[index]
        following = points[(index + 1) % len(points)]
        cx, cy = _coords(current)
        fx, fy = _coords(following)
        total += cx * fy - fx * cy
    return total / 2.0


def _coords(point) -> tuple[float, float]:
    """Return ``(x, y)`` for a NormPoint or a plain ``[x, y]`` sequence."""

    if hasattr(point, "x") and hasattr(point, "y"):
        return point.x, point.y
    return point[0], point[1]


def _is_simple(points: list[NormPoint]) -> bool:
    """Non-self-intersecting test via shapely, which the pipeline already depends on."""

    try:
        from shapely.geometry import Polygon

        return Polygon([_coords(point) for point in points]).is_simple
    except Exception:
        # Fallback: a degenerate polygon that still has positive area but cannot be
        # expressed by shapely is treated as non-simple (conservative).
        return False
