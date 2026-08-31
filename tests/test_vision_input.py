"""T04: frozen visual input artifact + versioned prompts + job key.

The same image with the same versions must produce the same artifact and job key;
any change to preprocessing, prompt or schema must change the key. EXIF and
magic-byte validation are exercised on synthetic bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest

from pano_platform.vision.input_artifact import (
    VisionInputError,
    detect_content_type,
    prepare_vision_input,
    prepare_vision_payload,
)
from pano_platform.vision.prompts import (
    PARSE_PROMPT_ID,
    REVIEW_PROMPT_ID,
    parse_prompt,
    review_prompt,
)
from pano_platform.vision.provider import to_data_uri

ROOT = Path(__file__).parents[1]


def _png_bytes(width=64, height=40, with_exif=False) -> bytes:
    image = np.full((height, width, 3), 255, np.uint8)
    image[10:30, 10:50] = (0, 0, 0)
    ok, encoded = cv2.imencode(".png", image)
    return encoded.tobytes()


def test_detect_content_type_validates_magic() -> None:
    png = _png_bytes()
    assert detect_content_type(png) == "image/png"

    jpeg = _jpeg_bytes()
    assert detect_content_type(jpeg) == "image/jpeg"

    with pytest.raises(VisionInputError):
        detect_content_type(b"not an image at all")


def _jpeg_bytes() -> bytes:
    image = np.full((40, 64, 3), 200, np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    return encoded.tobytes()


def test_prepare_rejects_non_image() -> None:
    with pytest.raises(VisionInputError):
        prepare_vision_input(b"\x00" * 16, "floorplan_parse_v1")


def test_prepare_rejects_empty_and_oversized() -> None:
    with pytest.raises(VisionInputError):
        prepare_vision_input(b"", "floorplan_parse_v1")
    with pytest.raises(VisionInputError):
        prepare_vision_input(b"\x89PNG" + b"\x00" * 10**7, "floorplan_parse_v1")


def test_same_input_same_version_produces_same_artifact_and_key() -> None:
    data = _png_bytes()
    a = prepare_vision_input(data, "floorplan_parse_v1")
    b = prepare_vision_input(data, "floorplan_parse_v1")
    assert a.jobKey == b.jobKey
    assert a.imageSha256 == b.imageSha256
    assert a.rawSha256 == b.rawSha256
    assert a.to_dict() == b.to_dict()


def test_different_prompt_version_changes_job_key() -> None:
    data = _png_bytes()
    a = prepare_vision_input(data, "floorplan_parse_v1")
    b = prepare_vision_input(data, "floorplan_parse_v2")
    assert a.jobKey != b.jobKey


def test_different_schema_version_changes_job_key() -> None:
    data = _png_bytes()
    a = prepare_vision_input(data, "floorplan_parse_v1", draft_schema_version="0.1.0")
    b = prepare_vision_input(data, "floorplan_parse_v1", draft_schema_version="0.2.0")
    assert a.jobKey != b.jobKey


def test_different_preprocessing_changes_job_key() -> None:
    data = _png_bytes()
    a = prepare_vision_input(data, "floorplan_parse_v1", crop_white_margins=True, enhance=True)
    b = prepare_vision_input(data, "floorplan_parse_v1", crop_white_margins=False, enhance=False)
    assert a.jobKey != b.jobKey


def test_artifact_records_dimensions_and_sha() -> None:
    data = _png_bytes(width=64, height=40)
    artifact = prepare_vision_input(data, "floorplan_parse_v1")
    assert artifact.width == 64
    assert artifact.height == 40
    assert artifact.contentType == "image/png"
    assert len(artifact.imageSha256) == 64
    assert artifact.preprocessingVersion == "v1"
    assert artifact.promptVersion == "floorplan_parse_v1"


def test_processed_hash_differs_after_white_margin_crop() -> None:
    data = _png_bytes()  # has white margins around the black block
    original_sha = hashlib.sha256(data).hexdigest()
    artifact = prepare_vision_input(data, "floorplan_parse_v1", crop_white_margins=True)
    # cropping away white page margin should change the processed bytes
    assert artifact.processedSha256 != original_sha


def test_provider_payload_matches_recorded_processed_hash() -> None:
    artifact, payload = prepare_vision_payload(
        _png_bytes(), "floorplan_parse_v1",
        crop_white_margins=False, enhance=False,
    )
    assert hashlib.sha256(payload).hexdigest() == artifact.processedSha256
    assert detect_content_type(payload) == artifact.contentType


def test_data_uri_uses_actual_jpeg_magic() -> None:
    assert to_data_uri(_jpeg_bytes()).startswith("data:image/jpeg;base64,")


def test_crop_artifact_records_processed_dimensions_and_offset() -> None:
    image = np.full((100, 140, 3), 255, np.uint8)
    image[20:80, 30:110] = 0
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    artifact, payload = prepare_vision_payload(encoded.tobytes(), "floorplan_parse_v1", crop_white_margins=True, enhance=False)
    assert artifact.cropOffsetX == 30 and artifact.cropOffsetY == 20
    assert artifact.processedWidth == 80 and artifact.processedHeight == 60
    assert payload


def test_prompts_load_and_are_versioned() -> None:
    parse = parse_prompt()
    review = review_prompt()
    assert PARSE_PROMPT_ID == "floorplan_parse_v1"
    assert REVIEW_PROMPT_ID == "floorplan_review_v1"
    # the parse prompt must demand normalised coordinates and strict enums
    assert "normalised" in parse.lower() or "NORMALISED" in parse
    assert "bedroom" in parse or "livingDining" in parse
    assert "[]" not in parse  # not a stub


def test_review_prompt_only_rates_given_candidates() -> None:
    review = review_prompt()
    assert "ALREADY-PROPOSED" in review or "already" in review.lower()
    assert "only to re-classify" in review or "only" in review.lower()
