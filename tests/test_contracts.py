import json
import hashlib
import struct
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from pano_platform.contracts import (
    ArtifactManifest,
    CaptureProfile,
    CommandEnvelope,
    CommandName,
    DatasetManifest,
    DerivedRoomSet,
    LayoutRevision,
    OpeningCandidateSet,
    PanoManifest,
    ScaleCalibrationRequest,
    canonical_sha256,
)
from pano_platform.facade import (
    CommandFacade,
    CommandNotImplementedError,
    accept_for_contract_test,
)

ROOT = Path(__file__).parents[1]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_layout_fixture_matches_json_schema_and_pydantic() -> None:
    document = load_json(ROOT / "tests/fixtures/layout-revision.valid.json")
    schema = load_json(ROOT / "schemas/layout-revision.schema.json")

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    model = LayoutRevision.model_validate(document)

    assert model.revision == 1
    assert len(model.edges) == 4


@pytest.mark.parametrize(
    ("fixture_name", "schema_name", "model_type"),
    [
        ("command-envelope.valid.json", "command-envelope.schema.json", CommandEnvelope),
        ("artifact-manifest.valid.json", "artifact-manifest.schema.json", ArtifactManifest),
        ("capture-profile.valid.json", "capture-profile.schema.json", CaptureProfile),
        (
            "../../apps/pano-viewer/public/demo/pano-manifest.json",
            "pano-manifest.schema.json",
            PanoManifest,
        ),
        (
            "../../apps/pano-viewer/public/demo/visual-manifest.json",
            "pano-manifest.schema.json",
            PanoManifest,
        ),
        (
            "../../apps/pano-viewer/public/demo/ai-manifest.json",
            "pano-manifest.schema.json",
            PanoManifest,
        ),
        (
            "../../datasets/manifest.json",
            "dataset-manifest.schema.json",
            DatasetManifest,
        ),
        (
            "scale-calibration.fp-001.json",
            "scale-calibration.schema.json",
            ScaleCalibrationRequest,
        ),
        (
            "../../datasets/derived/fp-001/layout-revision.manual-v2.json",
            "layout-revision.schema.json",
            LayoutRevision,
        ),
        (
            "../../datasets/derived/fp-001/rooms.manual-v2.json",
            "derived-rooms.schema.json",
            DerivedRoomSet,
        ),
        (
            "../../datasets/derived/fp-001/opening-candidates.auto-v2.json",
            "opening-candidates.schema.json",
            OpeningCandidateSet,
        ),
    ],
)
def test_contract_fixtures_match_schema_and_pydantic(
    fixture_name: str,
    schema_name: str,
    model_type: type,
) -> None:
    document = load_json((ROOT / "tests/fixtures" / fixture_name).resolve())
    schema = load_json(ROOT / "schemas" / schema_name)

    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    model_type.model_validate(document)


def test_golden_pano_manifest_matches_target_scene_matrix() -> None:
    document = load_json(ROOT / "apps/pano-viewer/public/demo/pano-manifest.json")
    manifest = PanoManifest.model_validate(document)

    assert len(manifest.scenes) == 8
    assert sum(scene.roomId == "room_living" for scene in manifest.scenes) == 5
    assert sum(scene.roomId == "room_kitchen" for scene in manifest.scenes) == 3
    assert all((scene.assets.original.width, scene.assets.original.height) == (6144, 3072) for scene in manifest.scenes)
    assert all((scene.assets.proxy2k.width, scene.assets.proxy2k.height) == (2048, 1024) for scene in manifest.scenes)
    assert all("3vjia" not in asset.url for scene in manifest.scenes for asset in (
        scene.assets.original,
        scene.assets.proxy2k,
        scene.assets.proxy1k,
    ))
    assert all("3vjia" not in scene.assets.thumbnailUrl for scene in manifest.scenes)


def test_dataset_manifest_files_match_recorded_evidence() -> None:
    document = load_json(ROOT / "datasets/manifest.json")
    manifest = DatasetManifest.model_validate(document)

    for sample in manifest.samples:
        path = ROOT / "datasets" / sample.image.file
        payload = path.read_bytes()
        width, height = struct.unpack(">II", payload[16:24])
        assert hashlib.sha256(payload).hexdigest() == sample.image.sha256
        assert len(payload) == sample.image.byteLength
        assert (width, height) == (sample.image.width, sample.image.height)

    decisions = [sample.expectedGate.decision.value for sample in manifest.samples]
    assert decisions.count("needsReview") == 1
    assert decisions.count("reject") == 2
    assert all(not sample.source.allowTraining for sample in manifest.samples)


@pytest.mark.parametrize(
    ("filename", "sha256", "width", "height"),
    [
        (
            "fp-001-living-visual-v1.png",
            "e5ea68cd010620effc5b407baf917ebaf9934596509fca208e377f07c5bf21e0",
            1774,
            887,
        ),
        (
            "fp-001-living-visual-v2-seam.png",
            "ca5bb09fad5fba335d56af578d0e52fe2362b5fccecbaf9bdda1f98e89361784",
            1774,
            887,
        ),
    ],
)
def test_visual_baseline_files_are_immutable(filename: str, sha256: str, width: int, height: int) -> None:
    payload = (ROOT / "datasets/generated" / filename).read_bytes()
    actual_width, actual_height = struct.unpack(">II", payload[16:24])
    assert hashlib.sha256(payload).hexdigest() == sha256
    assert (actual_width, actual_height) == (width, height)


def test_layout_content_hash_is_verified() -> None:
    document = load_json(ROOT / "tests/fixtures/layout-revision.valid.json")
    document["revision"] = 2

    with pytest.raises(ValidationError, match="contentHash"):
        LayoutRevision.model_validate(document)


def test_opening_cannot_extend_past_wall_edge() -> None:
    document = load_json(ROOT / "tests/fixtures/layout-revision.valid.json")
    invalid = deepcopy(document)
    invalid["openings"][0]["offset"] = 5000
    invalid["contentHash"] = canonical_sha256(
        {key: value for key, value in invalid.items() if key != "contentHash"}
    )

    with pytest.raises(ValidationError, match="exceeds edge"):
        LayoutRevision.model_validate(invalid)


def test_capture_profile_rejects_non_2_to_1_erp() -> None:
    with pytest.raises(ValidationError, match="2:1"):
        CaptureProfile.model_validate({
            **load_json(ROOT / "tests/fixtures/capture-profile.valid.json"),
            "actualErpWidth": 4095,
        })


@pytest.mark.parametrize("command", list(CommandName))
def test_command_envelope_supports_frozen_facade_commands(command: CommandName) -> None:
    envelope = CommandEnvelope(
        requestId=uuid4(),
        command=command,
        projectId="p001",
    )
    assert envelope.command is command


def test_facade_dispatches_only_registered_handlers() -> None:
    async def run() -> None:
        envelope = CommandEnvelope(
            requestId=uuid4(),
            command=CommandName.PARSE,
            projectId="p001",
        )
        facade = CommandFacade()

        with pytest.raises(CommandNotImplementedError):
            await facade.dispatch(envelope)

        facade.register(CommandName.PARSE, accept_for_contract_test)
        task = await facade.dispatch(envelope)
        assert task.status == "ACCEPTED"

    import asyncio

    asyncio.run(run())
