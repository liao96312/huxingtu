"""T06, F2: scale reconciliation (median/MAD) and CV cross-check."""

from __future__ import annotations

from pano_platform.floorplan.fusion import _reconcile_scale, _robust_median, fuse
from pano_platform.floorplan.wall_graph import WallGraph
from pano_platform.vision.contracts import RoomDraft, ScaleCandidate, VlmDraft


def graph_with_scale(mm_per_px: float = 8.5, axes=(100.0, 300.0, 500.0)) -> WallGraph:
    # a CV wall graph with a self-bootstrapped mm/px (from 110mm interior wall)
    return WallGraph(
        horizontal_axes=axes,
        vertical_axes=axes,
        segments=(),
        gaps=(),
        mm_per_px=mm_per_px,
    )


def test_single_vlm_scale_reconciles_to_bootstrap_when_no_cv_scale() -> None:
    draft = VlmDraft(
        scaleCandidates=[ScaleCandidate(lengthMm=3600, pixelStart=[0.1, 0.2], pixelEnd=[0.5, 0.2], text="3600", confidence=0.9)],
        rooms=[], openings=[], warnings=[],
    )
    graph = graph_with_scale(mm_per_px=0.0)  # no CV scale
    mm, status, evidence = _reconcile_scale(draft, graph, 1000, 1000)
    assert mm is not None
    # pixel distance 0.4 * width = 400px, 3600/400 = 9.0 mm/px
    assert abs(mm - 9.0) < 1e-6
    assert status in ("confirmed", "review")
    assert len(evidence) == 1


def test_vlm_scale_agrees_with_cv_scale_is_confirmed() -> None:
    # VLM reads 3600mm across a line of length 0.4 units. We synthesise a scenario
    # where vlm mm_per_px matches CV mm_per_px.
    # width=1000px -> pixelEnd[0]-pixelStart[0]=0.4 -> 400px, 3600/400 = 9.0 mm/px
    draft = VlmDraft(
        scaleCandidates=[ScaleCandidate(lengthMm=3600, pixelStart=[0.1, 0.2], pixelEnd=[0.5, 0.2], text="3600", confidence=0.9)],
        rooms=[], openings=[], warnings=[],
    )
    graph = graph_with_scale(mm_per_px=9.0)
    result = fuse(draft, graph, image_width=1000, image_height=1000)
    assert result.scale_status == "confirmed"
    assert result.mm_per_px is not None
    # relative error 0 -> agreed
    assert result.evidence.scale[0].agreed is True


def test_vlm_scale_disagrees_with_cv_scale_is_review() -> None:
    draft = VlmDraft(
        scaleCandidates=[ScaleCandidate(lengthMm=3600, pixelStart=[0.1, 0.2], pixelEnd=[0.5, 0.2], text="3600", confidence=0.9)],
        rooms=[], openings=[], warnings=[],
    )
    # CV says 9.0 mm/px but VLM says 3600/400 = 9.0 too... force a big gap:
    graph = graph_with_scale(mm_per_px=30.0)  # CV scale far from VLM 9.0
    result = fuse(draft, graph, image_width=1000, image_height=1000)
    assert result.scale_status == "review"
    assert "SCALE_UNCERTAIN" in result.evidence.reasonCodes


def test_agreeing_room_area_labels_override_wall_thickness_bootstrap() -> None:
    rooms = [
        RoomDraft(
            type="bedroom", areaLabelM2=4.0,
            polygon=[[0.1, 0.1], [0.2, 0.1], [0.2, 0.2], [0.1, 0.2]],
        ),
        RoomDraft(
            type="kitchen", areaLabelM2=16.0,
            polygon=[[0.3, 0.1], [0.5, 0.1], [0.5, 0.3], [0.3, 0.3]],
        ),
    ]
    result = fuse(VlmDraft(rooms=rooms), graph_with_scale(8.5), 1000, 1000)

    assert result.scale_status == "confirmed"
    assert abs(result.mm_per_px - 20.0) < 1e-6


def test_robust_median_rejects_outliers() -> None:
    observations = [9.0, 9.1, 8.9, 9.0, 50.0]  # 50.0 is a spurious outlier
    robust = _robust_median(observations)
    assert 8.8 < robust < 9.2


def test_multiple_scales_use_median_and_flag_consistency() -> None:
    candidates = [
        ScaleCandidate(lengthMm=3000, pixelStart=[0.0, 0.1], pixelEnd=[0.5, 0.1], text="3000", confidence=0.9),
        ScaleCandidate(lengthMm=6000, pixelStart=[0.0, 0.1], pixelEnd=[0.9, 0.1], text="6000", confidence=0.9),
        ScaleCandidate(lengthMm=3000, pixelStart=[0.0, 0.2], pixelEnd=[0.45, 0.2], text="3000", confidence=0.9),
    ]
    draft = VlmDraft(scaleCandidates=candidates, rooms=[], openings=[], warnings=[])
    # width 1000 -> 3000/500=6, 6000/900=6.666, 3000/450=6.666; median ~ 6.666 mm/px
    graph = graph_with_scale(mm_per_px=6.666)
    result = fuse(draft, graph, image_width=1000, image_height=1000)
    assert result.mm_per_px is not None
    assert abs(result.mm_per_px - 6.666) < 0.05
    assert result.scale_status == "confirmed"
    candidates = [
        ScaleCandidate(lengthMm=3000, pixelStart=[0.0, 0.1], pixelEnd=[0.5, 0.1], text="3000", confidence=0.9),
        ScaleCandidate(lengthMm=6000, pixelStart=[0.0, 0.1], pixelEnd=[0.9, 0.1], text="6000", confidence=0.9),
        ScaleCandidate(lengthMm=3000, pixelStart=[0.0, 0.2], pixelEnd=[0.45, 0.2], text="3000", confidence=0.9),
    ]
    draft = VlmDraft(scaleCandidates=candidates, rooms=[], openings=[], warnings=[])
    # width 1000 -> 3000/500=6, 6000/900=6.666, 3000/450=6.666; median ~ 6.666 mm/px
    graph = graph_with_scale(mm_per_px=6.666)
    result = fuse(draft, graph, image_width=1000, image_height=1000)
    assert result.mm_per_px is not None
    assert abs(result.mm_per_px - 6.666) < 0.05
    assert result.scale_status == "confirmed"
