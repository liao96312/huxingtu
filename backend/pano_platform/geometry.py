from __future__ import annotations

from math import isclose
from statistics import median

from .contracts import (
    CalibrationStatus,
    Point2D,
    ScaleCalibrationRequest,
    ScaleCalibrationResult,
)

AffineMatrix = tuple[float, float, float, float, float, float, float, float, float]


def transform_point(matrix: AffineMatrix, point: Point2D) -> Point2D:
    a, b, c, d, e, f, _, _, _ = matrix
    return Point2D(x=a * point.x + b * point.y + c, y=d * point.x + e * point.y + f)


def invert_affine(matrix: AffineMatrix) -> AffineMatrix:
    a, b, c, d, e, f, g, h, i = matrix
    if not (isclose(g, 0) and isclose(h, 0) and isclose(i, 1)):
        raise ValueError("matrix must be a 2D affine transform")
    determinant = a * e - b * d
    if isclose(determinant, 0, abs_tol=1e-12):
        raise ValueError("affine transform is not invertible")
    return (
        e / determinant,
        -b / determinant,
        (b * f - e * c) / determinant,
        -d / determinant,
        a / determinant,
        (d * c - a * f) / determinant,
        0,
        0,
        1,
    )


def calibrate_scale(request: ScaleCalibrationRequest) -> ScaleCalibrationResult:
    scales = [item.worldDistanceMm / item.pixelDistance for item in request.observations]
    mm_per_pixel = median(scales)
    residuals = [abs(scale - mm_per_pixel) / mm_per_pixel for scale in scales]
    max_residual = max(residuals)
    reason_codes: list[str] = []
    if len(scales) < 2:
        reason_codes.append("INSUFFICIENT_DIMENSION_OBSERVATIONS")
    if max_residual > request.maxRelativeResidual:
        reason_codes.append("INCONSISTENT_DIMENSIONS")
    status = CalibrationStatus.NEEDS_REVIEW if reason_codes else CalibrationStatus.CONFIRMED

    # Image pixels grow downward; the business XY plane grows upward.
    x_offset = request.worldOrigin.x - mm_per_pixel * request.pixelOrigin.x
    y_offset = request.worldOrigin.y + mm_per_pixel * request.pixelOrigin.y
    transform: AffineMatrix = (
        mm_per_pixel,
        0,
        x_offset,
        0,
        -mm_per_pixel,
        y_offset,
        0,
        0,
        1,
    )
    return ScaleCalibrationResult(
        sampleId=request.sampleId,
        status=status,
        mmPerPixel=mm_per_pixel,
        maxRelativeResidual=max_residual,
        observationCount=len(scales),
        pixelToWorldTransform=transform,
        reasonCodes=reason_codes,
    )
