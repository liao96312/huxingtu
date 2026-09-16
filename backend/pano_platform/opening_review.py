from __future__ import annotations

import json
from pathlib import Path

from .contracts import (
    DerivedRoomSet,
    LayoutRevision,
    Opening,
    OpeningCandidateKind,
    OpeningCandidateSet,
    OpeningReviewRequest,
    OpeningReviewResult,
    canonical_sha256,
)

DATASETS = Path(__file__).resolve().parents[2] / "datasets" / "derived"


def load_opening_candidates(project_id: str) -> OpeningCandidateSet:
    path = DATASETS / ("fp-001" if project_id == "fp001" else project_id) / "opening-candidates.auto-v2.json"
    return OpeningCandidateSet.model_validate_json(path.read_text(encoding="utf-8"))


def load_latest_reviewed_layout(project_id: str) -> LayoutRevision:
    folder = DATASETS / ("fp-001" if project_id == "fp001" else project_id)
    layouts = [LayoutRevision.model_validate_json(path.read_text(encoding="utf-8")) for path in folder.glob("layout-revision.reviewed-v*.json")]
    if not layouts:
        raise FileNotFoundError("reviewed layout not found")
    return max(layouts, key=lambda layout: layout.revision)


def load_derived_rooms(project_id: str) -> DerivedRoomSet:
    folder = DATASETS / ("fp-001" if project_id == "fp001" else project_id)
    paths = sorted(folder.glob("rooms.*.json"))
    if not paths:
        raise FileNotFoundError("derived rooms not found")
    return DerivedRoomSet.model_validate_json(paths[-1].read_text(encoding="utf-8"))


def complete_opening_review(project_id: str, request: OpeningReviewRequest) -> OpeningReviewResult:
    folder = DATASETS / ("fp-001" if project_id == "fp001" else project_id)
    candidates = load_opening_candidates(project_id)
    decisions = {item.candidateId: item.decision for item in request.decisions}
    expected = {item.id for item in candidates.candidates}
    if decisions.keys() != expected:
        missing = sorted(expected - decisions.keys())
        unknown = sorted(decisions.keys() - expected)
        raise ValueError(f"review must cover every candidate; missing={missing}, unknown={unknown}")

    source_path = folder / "layout-revision.manual-v2.json"
    source = LayoutRevision.model_validate_json(source_path.read_text(encoding="utf-8"))
    payload = source.model_dump(mode="json", exclude={"contentHash"})
    payload["id"] = f"{source.id.rsplit('_r', 1)[0]}_r{source.revision + 1}"
    payload["revision"] = source.revision + 1
    payload["openings"] = [
        Opening(
            id=candidate.id.replace("candidate", "confirmed"),
            type=candidate.kind.value,
            wallEdgeId=candidate.wallEdgeId,
            offset=candidate.offset,
            width=candidate.width,
            height=2200 if candidate.kind is OpeningCandidateKind.DOOR else 1500,
            sill=None if candidate.kind is OpeningCandidateKind.DOOR else 600,
            confidence=candidate.confidence,
        ).model_dump(mode="json", exclude_none=True)
        for candidate in candidates.candidates
        if decisions[candidate.id] == "confirmed"
    ]
    payload["contentHash"] = canonical_sha256(payload)
    reviewed = LayoutRevision.model_validate(payload)

    output = folder / "layout-revision.reviewed-v3.json"
    if output.exists():
        existing = LayoutRevision.model_validate_json(output.read_text(encoding="utf-8"))
        if existing.contentHash != reviewed.contentHash:
            raise ValueError("this source revision already has a different completed review")
    audit_payload = {
        "schemaVersion": "0.1.0",
        "projectId": project_id,
        "candidateSetHash": candidates.contentHash,
        "sourceLayoutHash": source.contentHash,
        "decisions": [item.model_dump(mode="json") for item in request.decisions],
        "outputLayoutHash": reviewed.contentHash,
    }
    audit_payload["contentHash"] = canonical_sha256(audit_payload)

    temporary = output.with_suffix(".tmp")
    temporary.write_text(reviewed.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    temporary.replace(output)
    audit = folder / "opening-review.v3.json"
    audit_temporary = audit.with_suffix(".tmp")
    audit_temporary.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    audit_temporary.replace(audit)

    confirmed = len(reviewed.openings)
    return OpeningReviewResult(
        projectId=project_id,
        sourceLayoutRevisionId=source.id,
        layoutRevisionId=reviewed.id,
        confirmedCount=confirmed,
        rejectedCount=len(candidates.candidates) - confirmed,
    )
