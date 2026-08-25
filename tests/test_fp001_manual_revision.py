import json
import struct
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from pano_platform.contracts import DerivedRoomSet, LayoutRevision, OpeningCandidateSet

ROOT = Path(__file__).parents[1]
DERIVED = ROOT / "datasets/derived/fp-001"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_manual_layout_revision_has_expected_geometry() -> None:
    layout = LayoutRevision.model_validate(load_json(DERIVED / "layout-revision.manual-v2.json"))

    assert layout.revision == 2
    assert len(layout.nodes) > 17
    assert len(layout.edges) > 21
    assert len(layout.openings) == 0
    assert len(layout.roomAnnotations) == 5


def test_derived_rooms_are_valid_non_overlapping_and_plausible() -> None:
    room_set = DerivedRoomSet.model_validate(load_json(DERIVED / "rooms.manual-v2.json"))
    polygons = [Polygon([(point.x, point.y) for point in room.polygon]) for room in room_set.rooms]

    assert {room.type for room in room_set.rooms} == {
        "bedroom",
        "bathroom",
        "kitchen",
        "living_dining",
    }
    assert all(polygon.is_valid for polygon in polygons)
    assert all(polygon.area > 1_000_000 for polygon in polygons)
    for index, polygon in enumerate(polygons):
        for other in polygons[index + 1 :]:
            assert polygon.intersection(other).area == pytest.approx(0, abs=1e-6)

    total_area = sum(room.areaMm2 for room in room_set.rooms)
    assert 48_000_000 < total_area < 51_000_000


def test_debug_overlay_matches_source_dimensions() -> None:
    source = (ROOT / "datasets/source/fp-001.png").read_bytes()
    overlay = (ROOT / "datasets/debug/fp-001-layout-overlay-v2-openings.png").read_bytes()

    assert struct.unpack(">II", source[16:24]) == (621, 450)
    assert struct.unpack(">II", overlay[16:24]) == (621, 450)


def test_manual_revision_quality_evidence_requires_review() -> None:
    quality = load_json(DERIVED / "quality.manual-v2.json")

    assert quality["decision"] == "needsReview"
    assert quality["wallAlignmentCoverage"] > 0.90
    assert quality["wallAlignmentSamples"] > 1000
    assert "OPENING_CANDIDATES_REQUIRE_REVIEW" in quality["reasonCodes"]


def test_openings_are_candidates_not_geometry_facts() -> None:
    candidates = OpeningCandidateSet.model_validate(
        load_json(DERIVED / "opening-candidates.auto-v2.json")
    )

    assert candidates.status == "needsReview"
    assert any(candidate.kind.value == "door" for candidate in candidates.candidates)
    assert any(candidate.kind.value == "window" for candidate in candidates.candidates)
    assert all(candidate.sideARoomId != candidate.sideBRoomId for candidate in candidates.candidates)
