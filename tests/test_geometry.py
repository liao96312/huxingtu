import json
from pathlib import Path

import pytest

from pano_platform.contracts import Point2D, ScaleCalibrationRequest
from pano_platform.geometry import calibrate_scale, invert_affine, transform_point

ROOT = Path(__file__).parents[1]


def load_fp001_request() -> ScaleCalibrationRequest:
    document = json.loads(
        (ROOT / "tests/fixtures/scale-calibration.fp-001.json").read_text(encoding="utf-8")
    )
    return ScaleCalibrationRequest.model_validate(document)


def test_fp001_scale_is_confirmed() -> None:
    result = calibrate_scale(load_fp001_request())

    assert result.status.value == "confirmed"
    assert 28.8 < result.mmPerPixel < 29.1
    assert result.maxRelativeResidual < 0.03
    assert result.reasonCodes == []


def test_pixel_world_roundtrip_is_subpixel() -> None:
    result = calibrate_scale(load_fp001_request())
    inverse = invert_affine(result.pixelToWorldTransform)

    for source in (Point2D(x=200, y=369), Point2D(x=491, y=154), Point2D(x=310, y=280)):
        restored = transform_point(inverse, transform_point(result.pixelToWorldTransform, source))
        assert restored.x == pytest.approx(source.x, abs=1e-9)
        assert restored.y == pytest.approx(source.y, abs=1e-9)


def test_inconsistent_dimensions_require_review() -> None:
    request = load_fp001_request().model_copy(deep=True)
    request.observations[1].worldDistanceMm = 9000

    result = calibrate_scale(request)

    assert result.status.value == "needsReview"
    assert "INCONSISTENT_DIMENSIONS" in result.reasonCodes
