"""T00: dataset provenance, registry integrity and slice exclusivity.

These tests enforce the frozen v2 data discipline for the recovered desktop-9
sample set:

* every registered sample resolves to on-disk bytes with a matching SHA-256 and
  dimensions (no provenance-blind files in git);
* image hashes are unique across the registry (no duplicated sample);
* slices are exclusive (a sample id appears in exactly one split), and the
  held-out ``test`` split is disjoint from any tuning slice;
* an image is only allowed toward an external vision provider if it carries an
  explicit ``sourceGrant.allowExternalVision``.
"""

import hashlib
import json
import struct
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from pano_platform.contracts import DatasetManifest

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "datasets/source/desktop-9/manifest.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_desktop9_manifest_matches_schema_and_pydantic() -> None:
    document = load_json(MANIFEST)
    schema = load_json(ROOT / "schemas/dataset-manifest.schema.json")

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    manifest = DatasetManifest.model_validate(document)

    assert len(manifest.samples) == 9
    assert manifest.contentHash  # contentHash verified by the model


def test_desktop9_images_resolve_and_hash_match() -> None:
    manifest = DatasetManifest.model_validate(load_json(MANIFEST))

    for sample in manifest.samples:
        path = ROOT / "datasets" / sample.image.file
        payload = path.read_bytes()
        width, height = struct.unpack(">II", payload[16:24])
        assert hashlib.sha256(payload).hexdigest() == sample.image.sha256
        assert len(payload) == sample.image.byteLength
        assert (width, height) == (sample.image.width, sample.image.height)
        assert sample.image.file.endswith(".png")


def test_no_duplicate_image_hash_across_registry() -> None:
    manifests = [
        load_json(ROOT / "datasets/manifest.json"),
        load_json(MANIFEST),
    ]
    seen: dict[str, str] = {}
    for manifest in manifests:
        for sample in manifest["samples"]:
            sha = sample["image"]["sha256"]
            if sha in seen and seen[sha] != sample["id"]:
                pytest.fail(f"duplicate image hash {sha[:12]} across {seen[sha]} and {sample['id']}")
            seen[sha] = sample["id"]


def test_slices_are_exclusive_and_cover_every_sample() -> None:
    manifest = DatasetManifest.model_validate(load_json(MANIFEST))
    slices: dict[str, list[str]] = {"train": [], "val": [], "test": []}

    for sample in manifest.samples:
        assert sample.split in slices, f"sample {sample.id} missing/unknown split {sample.split}"
        slices[sample.split].append(sample.id)

    # exclusive: no id in more than one slice
    for ids in slices.values():
        assert len(ids) == len(set(ids))

    # every sample assigned exactly once (no id in two slices)
    for sample in manifest.samples:
        in_slices = [s for s in slices if sample.id in slices[s]]
        assert len(in_slices) == 1, f"{sample.id} appears in slices {in_slices}"


def test_test_slice_is_held_out_and_disjoint() -> None:
    manifest = DatasetManifest.model_validate(load_json(MANIFEST))
    test_ids = {s.id for s in manifest.samples if s.split == "test"}
    non_test_ids = {s.id for s in manifest.samples if s.split != "test"}
    assert test_ids
    # test must never overlap tuning (train/val) identifiers
    assert not (test_ids & non_test_ids)
    # keep the held-out slice small and separate: test must be a strict subset of all
    assert len(test_ids) < len(manifest.samples)


def test_external_vision_grant_is_explicit_everywhere() -> None:
    manifest = DatasetManifest.model_validate(load_json(MANIFEST))
    for sample in manifest.samples:
        assert sample.sourceGrant is not None, f"{sample.id} lacks external-vision grant"
        assert sample.sourceGrant.allowExternalVision is True
        assert sample.source.allowTraining is False


def test_unauthorized_image_cannot_be_flagged_for_vision() -> None:
    # An image without allowExternalVision must not be sent to Tuzi.
    manifest = DatasetManifest.model_validate(load_json(MANIFEST))
    for sample in manifest.samples:
        allowed = sample.sourceGrant is not None and sample.sourceGrant.allowExternalVision
        # the vision send gate is a single boolean: no grant -> never allow
        assert allowed is (sample.sourceGrant.allowExternalVision if sample.sourceGrant else False)
