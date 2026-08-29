"""T02: frozen VLM input/output contracts.

A valid draft must satisfy both the JSON Schema and the Pydantic model, and
invalid drafts must be rejected with a clear reason. Coordinates are normalised
``[x, y]`` arrays. No production artifact may ingest a draft that fails here.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from pano_platform.vision.contracts import (
    OpeningDraft,
    RoomDraft,
    VisionParseRequest,
    VisionRunEvidence,
    VlmDraft,
    _signed_area,
)

ROOT = Path(__file__).parents[1]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_with_schema(document: dict, schema_path: str) -> None:
    schema = load_json(ROOT / "schemas" / schema_path)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)


def test_valid_vlm_draft_fixture_passes_schema_and_pydantic() -> None:
    document = load_json(ROOT / "tests/fixtures/vlm-draft.valid.json")
    validate_with_schema(document, "vlm-draft.schema.json")
    draft = VlmDraft.model_validate(document)

    assert len(draft.scaleCandidates) == 1
    assert len(draft.rooms) == 2
    assert draft.rooms[0].type.value == "livingDining"
    assert len(draft.openings) == 1
    # coordinates parse as normalised points with x/y accessors
    assert draft.rooms[0].polygon[0].x == pytest.approx(0.12)
    assert draft.openings[0].pixelStart.y == pytest.approx(0.25)


def test_parse_request_and_evidence_validate_against_schema() -> None:
    request = VisionParseRequest(
        imageSha256="a" * 64, provider="tuzi", model="gemini-2.5-pro",
        promptVersion="floorplan_parse_v1", draftSchemaVersion="0.1.0",
        sourceWidth=799, sourceHeight=799,
    )
    validate_with_schema(request.model_dump(exclude_none=True), "vision-parse-request.schema.json")

    evidence = VisionRunEvidence(
        provider="tuzi", endpoint="/v1/chat/completions", requestedModel="gemini-3.5-flash",
        actualModel="gemini-3.5-flash", providerRequestId="req_1",
        promptVersion="floorplan_parse_v1", draftSchemaVersion="0.1.0",
        imageSha256="b" * 62 + "ab", latencyMs=123, usage={"input_tokens": 10},
    )
    validate_with_schema(evidence.model_dump(exclude_none=True), "vision-run-evidence.schema.json")


def test_out_of_range_coordinate_rejected() -> None:
    with pytest.raises(ValidationError, match="within"):
        RoomDraft(
            type="bedroom",
            polygon=[[1.5, 0.1], [0.5, 0.1], [0.5, 0.5]],
            confidence=0.9,
        )


def test_negative_coordinate_rejected() -> None:
    with pytest.raises(ValidationError, match="within"):
        RoomDraft(
            type="bedroom",
            polygon=[[-0.1, 0.1], [0.5, 0.1], [0.5, 0.5]],
            confidence=0.9,
        )


def test_self_intersecting_polygon_rejected() -> None:
    # A bow-tie (自交) is rejected; it may surface as degenerate (zero area) or
    # self-intersecting depending on which validator fires first.
    with pytest.raises(ValidationError):
        RoomDraft(
            type="bedroom",
            polygon=[[0.1, 0.1], [0.6, 0.6], [0.1, 0.6], [0.6, 0.1]],
            confidence=0.9,
        )


def test_unknown_room_enum_rejected() -> None:
    with pytest.raises(ValidationError):
        RoomDraft(
            type="garage",
            polygon=[[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]],
            confidence=0.9,
        )


def test_unknown_opening_enum_rejected() -> None:
    with pytest.raises(ValidationError):
        OpeningDraft(
            type="sliding",
            pixelStart=[0.1, 0.2], pixelEnd=[0.1, 0.3], confidence=0.8,
        )


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        RoomDraft.model_validate({
            "type": "bedroom",
            "polygon": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]],
            "confidence": 0.9,
            "colour": "red",
        })


def test_nan_coordinate_rejected() -> None:
    with pytest.raises(ValidationError):
        RoomDraft(
            type="bedroom",
            polygon=[[math.nan, 0.1], [0.5, 0.1], [0.5, 0.5]],
            confidence=0.9,
        )


def test_room_limit_overflow_rejected() -> None:
    many_rooms = [
        {"type": "bedroom", "polygon": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]], "confidence": 0.5}
        for _ in range(65)
    ]
    with pytest.raises(ValidationError, match="at most"):
        VlmDraft(rooms=many_rooms, openings=[], scaleCandidates=[], warnings=[])


def test_empty_draft_rejected() -> None:
    # Empty and unexplained -> rejected
    with pytest.raises(ValidationError, match="at least one"):
        VlmDraft(rooms=[], openings=[], scaleCandidates=[], warnings=[])


def test_empty_draft_with_warning_is_accepted() -> None:
    # Empty but explained (e.g. "not a floorplan") -> acceptable low-confidence
    draft = VlmDraft(rooms=[], openings=[], scaleCandidates=[], warnings=["not a floorplan"])
    assert len(draft.warnings) == 1


def test_missing_provider_confidence_is_conservatively_zero() -> None:
    room = RoomDraft(
        type="bedroom",
        polygon=[[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]],
    )
    opening = OpeningDraft(
        type="door", pixelStart=[0.1, 0.2], pixelEnd=[0.1, 0.3]
    )
    assert room.confidence == 0
    assert opening.confidence == 0


def test_opening_endpoints_must_differ() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        OpeningDraft(
            type="door",
            pixelStart=[0.1, 0.2], pixelEnd=[0.1, 0.2], confidence=0.8,
        )


def test_room_polygon_repeats_first_point_rejected() -> None:
    with pytest.raises(ValidationError, match="repeat its first point"):
        RoomDraft(
            type="bedroom",
            polygon=[[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.1]],
            confidence=0.9,
        )


def test_signed_area_sign_and_orientation_independent() -> None:
    # Both orientations produce the same absolute area.
    ccw = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]
    cw = list(reversed(ccw))
    assert abs(_signed_area(ccw)) == pytest.approx(abs(_signed_area(cw)))
