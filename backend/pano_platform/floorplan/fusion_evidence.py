"""Evidence recorded during fusion, so a reviewer can see why a VLM draft
became (or did not become) a candidate geometry result.

Nothing in the evidence carries secrets. Coordinates are kept in source-image
pixels after de-normalisation; a separate ``snapped`` boolean and per-item reason
codes show whether a vertex landed on a real wall axis or was left for review.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScaleEvidence:
    """How a VLM scale candidate was reconciled with the CV wall-thickness scale."""

    candidateId: str
    lengthMm: float
    pixelDistance: float
    vlmMmPerPixel: float
    cvMmPerPixel: float
    relativeError: float
    agreed: bool
    reasonCodes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RoomSnapEvidence:
    """Per-room fusion diagnostics."""

    roomId: str
    polygon: tuple[tuple[float, float], ...]
    snappedPolygon: tuple[tuple[float, float], ...]
    areaPx2: float
    areaMm2: float
    areaLabelM2: float | None
    areaRelativeError: float | None
    snappedVertexCount: int
    totalVertexCount: int
    status: str  # "ok" | "review"
    reasonCodes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FusionEvidence:
    """Aggregate fusion evidence for one draft."""

    scale: tuple[ScaleEvidence, ...]
    rooms: tuple[RoomSnapEvidence, ...]
    mmPerPixel: float | None
    scaleStatus: str  # "confirmed" | "bootstrap" | "review"
    reasonCodes: tuple[str, ...] = field(default_factory=tuple)
