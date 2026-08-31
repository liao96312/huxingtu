"""Stage T04: freeze the visual input artifact sent to the vision provider.

Before any image reaches the provider it is normalised into a reproducible
``VisionInputArtifact``:

* EXIF removed (re-encode without metadata bytes);
* MIME and magic bytes verified;
* pixel size, byte size and decoded content bounded;
* optional white-margin crop and structure enhancement frozen to a version.

Every mix of (image, preprocessing version, prompt version, draft schema version)
yields a deterministic ``jobKey``. Identical inputs produce the same artifact and
the same key; any change in preprocessing, prompt or schema changes the key, so
cached vision results are never reused across versions.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from .contracts import vlm_draft_schema

PREPROCESSING_VERSION = "v1"
MAX_PIXELS = 40_000_000  # ~ 6326 x 6326
MAX_BYTES = 10 * 1024 * 1024

_CONTENT_TYPES: dict[tuple[int, int], str] = {
    (0xFF, 0xD8): "image/jpeg",
    (0x89, 0x50): "image/png",
    (0x52, 0x49): "image/webp",  # RIFF....WEBP
}


@dataclass(frozen=True)
class VisionInputArtifact:
    """A validated, deterministic visual input record."""

    imageSha256: str
    width: int
    height: int
    contentType: Literal["image/png", "image/jpeg", "image/webp"]
    byteLength: int
    rawSha256: str
    processedSha256: str
    preprocessingVersion: str
    promptVersion: str
    draftSchemaVersion: str
    jobKey: str

    def to_dict(self) -> dict:
        return {
            "imageSha256": self.imageSha256,
            "width": self.width,
            "height": self.height,
            "contentType": self.contentType,
            "byteLength": self.byteLength,
            "rawSha256": self.rawSha256,
            "processedSha256": self.processedSha256,
            "preprocessingVersion": self.preprocessingVersion,
            "promptVersion": self.promptVersion,
            "draftSchemaVersion": self.draftSchemaVersion,
            "jobKey": self.jobKey,
        }


class VisionInputError(Exception):
    """Raised for malformed or out-of-bound visual input."""


def prepare_vision_input(
    image_bytes: bytes,
    prompt_version: str,
    draft_schema_version: str | None = None,
    *,
    crop_white_margins: bool = True,
    enhance: bool = True,
) -> VisionInputArtifact:
    """Normalise an image for the vision path and derive its deterministic job key."""

    artifact, _ = prepare_vision_payload(
        image_bytes, prompt_version, draft_schema_version,
        crop_white_margins=crop_white_margins, enhance=enhance,
    )
    return artifact


def prepare_vision_payload(
    image_bytes: bytes,
    prompt_version: str,
    draft_schema_version: str | None = None,
    *,
    crop_white_margins: bool = True,
    enhance: bool = True,
) -> tuple[VisionInputArtifact, bytes]:
    """Return the auditable input record and the exact sanitized provider bytes."""

    if len(image_bytes) > MAX_BYTES:
        raise VisionInputError("image exceeds byte limit")
    if len(image_bytes) == 0:
        raise VisionInputError("image is empty")

    content_type = detect_content_type(image_bytes)
    raw_sha = sha256(image_bytes)

    decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise VisionInputError("decoding failed")
    height, width = decoded.shape[:2]
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise VisionInputError("image pixel size out of bounds")

    cleaned = _strip_exif(image_bytes, content_type)

    processed = cleaned
    if crop_white_margins:
        processed = _crop_white_margins(image_bytes)
    if enhance:
        processed = _structure_enhance(processed)

    processed_sha = sha256(processed)

    schema_version = draft_schema_version or _schema_version(draft_schema_version)
    job_key = sha256_hash_of({
        "imageSha256": raw_sha,
        "preprocessingVersion": PREPROCESSING_VERSION,
        "cropWhiteMargins": crop_white_margins,
        "enhance": enhance,
        "promptVersion": prompt_version,
        "draftSchemaVersion": schema_version,
    })

    artifact = VisionInputArtifact(
        imageSha256=raw_sha,
        width=width,
        height=height,
        contentType=content_type,
        byteLength=len(cleaned),
        rawSha256=raw_sha,
        processedSha256=processed_sha,
        preprocessingVersion=PREPROCESSING_VERSION,
        promptVersion=prompt_version,
        draftSchemaVersion=schema_version,
        jobKey=job_key,
    )
    return artifact, processed


def detect_content_type(image_bytes: bytes) -> str:
    if len(image_bytes) < 12:
        raise VisionInputError("too small to be an image")
    first, second = image_bytes[0], image_bytes[1]
    kind = _CONTENT_TYPES.get((first, second))
    if kind is None:
        raise VisionInputError("unsupported image MIME (magic bytes mismatch)")
    if kind == "image/webp":
        # RIFF....WEBP: bytes 0-3 RIFF, 8-11 WEBP
        if image_bytes[8:12] != b"WEBP":
            raise VisionInputError("RIFF header without WEBP signature")
    return kind


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_hash_of(value: object) -> str:
    import json

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strip_exif(image_bytes: bytes, content_type: str) -> bytes:
    """Re-encode without metadata so no EXIF/GPS reaches the provider."""

    decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if content_type == "image/jpeg":
        ok, encoded = cv2.imencode(".jpg", decoded, [cv2.IMWRITE_JPEG_QUALITY, 95])
    else:
        ok, encoded = cv2.imencode(".png", decoded)
    if not ok:
        raise VisionInputError("re-encode failed while stripping EXIF")
    return encoded.tobytes()


def _crop_white_margins(image_bytes: bytes) -> bytes:
    """Crop near-white margins and a thin frame, then re-encode.

    Freezing the crop keeps the provider's normalized coordinates anchored to the
    drawing rather than to the surrounding white page, which stabilises the
    [0,1] coordinate mapping.
    """

    decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(decoded, cv2.COLOR_BGR2GRAY)
    non_white = gray < 245
    ys, xs = np.where(non_white)
    if xs.size == 0 or ys.size == 0:
        return image_bytes
    top = int(ys.min()); bottom = int(ys.max()) + 1
    left = int(xs.min()); right = int(xs.max()) + 1
    cropped = decoded[top:bottom, left:right]
    if cropped.shape[0] == 0 or cropped.shape[1] == 0:
        return image_bytes
    ok, encoded = cv2.imencode(".png", cropped)
    return encoded.tobytes() if ok else image_bytes


def _structure_enhance(image_bytes: bytes) -> bytes:
    """Optional contrast/sharpening pass, frozen so it is deterministic."""

    decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(decoded, cv2.COLOR_BGR2GRAY)
    # light adaptive contrast to lift thin vector strokes
    enhanced = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    ok, encoded = cv2.imencode(".png", enhanced)
    return encoded.tobytes() if ok else image_bytes


def _schema_version(draft_schema_version: str | None) -> str:
    """Return a stable schema version string for job-keying."""

    if draft_schema_version:
        return draft_schema_version
    document = vlm_draft_schema()
    return document.get("$id", "0.1.0").rsplit("/", 1)[-1]


def image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    """Read (width, height) from headers without a full decode, for quick checks."""

    if len(image_bytes) < 24:
        raise VisionInputError("too small to read dimensions")
    content_type = detect_content_type(image_bytes)
    if content_type == "image/png":
        width, height = struct.unpack(">II", image_bytes[16:24])
        return width, height
    if content_type == "image/jpeg":
        decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        height, width = decoded.shape[:2]
        return width, height
    decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    height, width = decoded.shape[:2]
    return width, height
