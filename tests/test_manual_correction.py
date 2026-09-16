from pathlib import Path

import pytest

from pano_platform.contracts import LayoutRevision, OpeningType, Point2D
from pano_platform.manual_correction import add_pixel_annotated_opening


ROOT = Path(__file__).resolve().parents[1]


def source_layout() -> LayoutRevision:
    return LayoutRevision.model_validate_json(
        (ROOT / "datasets/derived/fp-001/layout-revision.reviewed-v3.json").read_text(encoding="utf-8")
    )


def test_red_box_annotation_projects_to_upper_kitchen_wall() -> None:
    corrected = add_pixel_annotated_opening(
        source_layout(), "manual-door", OpeningType.DOOR, Point2D(x=433, y=203), Point2D(x=433, y=232)
    )
    opening = corrected.openings[-1]
    assert opening.wallEdgeId == "w_kitchen_left_upper"
    assert opening.width == pytest.approx(838.769231, abs=0.01)
    assert opening.offset == pytest.approx(57.846154, abs=0.01)


def test_annotation_must_be_near_a_wall() -> None:
    with pytest.raises(ValueError, match="too far"):
        add_pixel_annotated_opening(
            source_layout(), "bad-door", OpeningType.DOOR, Point2D(x=50, y=50), Point2D(x=50, y=80)
        )
