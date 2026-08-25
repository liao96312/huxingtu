import json
from pathlib import Path

from pano_platform.contracts import ArtifactManifest, ArtifactType


ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "datasets/derived/fp-001/panorama/living-main/ai/living-main-gpt-image-2-v1.structure-qa.json"


def test_structure_qa_is_explicitly_a_proxy_not_fake_iou() -> None:
    qa = json.loads(QA.read_text(encoding="utf-8"))
    assert qa["method"] == "semantic-boundary-to-canny-chamfer-v1"
    assert 0 <= qa["allStructureEdges"]["recallWithin6px"] <= 1
    assert 0 <= qa["doorWindowEdges"]["recallWithin6px"] <= 1
    assert qa["regionIoU"] is None
    assert qa["edgeF1"] is None
    assert qa["status"] == "needsReview"
    assert "AI_SEMANTIC_SEGMENTATION_REQUIRED_FOR_IOU_AND_F1" in qa["reasonCodes"]
    manifest = ArtifactManifest.model_validate_json(
        (QA.parent / "qualityReport.artifact-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.artifactType is ArtifactType.QUALITY_REPORT


def test_kitchen_structure_qa_remains_needs_review() -> None:
    path = ROOT / "datasets/derived/fp-001/panorama/kitchen-main/ai/kitchen-main-gpt-image-2-v1.structure-qa.json"
    qa = json.loads(path.read_text(encoding="utf-8"))
    assert qa["roomId"] == "room_kitchen"
    assert qa["status"] == "needsReview"
    assert qa["edgeF1"] is None and qa["regionIoU"] is None


def test_cream_style_structure_qa_is_comparable() -> None:
    path = ROOT / "datasets/derived/fp-001/panorama/living-main/ai/living-main-gpt-image-2-cream-v1.structure-qa.json"
    qa = json.loads(path.read_text(encoding="utf-8"))
    assert qa["status"] == "needsReview"
    assert qa["allStructureEdges"]["recallWithin6px"] > 0
    assert qa["doorWindowEdges"]["recallWithin6px"] > 0
