import asyncio
import base64
from uuid import uuid4

import httpx

from pano_platform.main import app


def request(method: str, path: str, **kwargs) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def test_health() -> None:
    response = request("GET", "/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0",
        "schemaVersion": "0.1.0",
    }


def test_pipeline_styles_are_served_from_frozen_catalog() -> None:
    response = request("GET", "/api/v1/pipeline/styles")

    assert response.status_code == 200
    assert response.json()["styles"][:4] == [
        {"id": "modern", "label": "现代"},
        {"id": "chinese", "label": "中式"},
        {"id": "scandinavian", "label": "北欧"},
        {"id": "cream", "label": "奶油风"},
    ]


def test_parse_command_persists_queryable_result(tmp_path, monkeypatch) -> None:
    import cv2
    import numpy as np

    image = np.full((40, 64, 3), 255, np.uint8)
    image[10:30, 10:50] = 0
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    monkeypatch.setenv("PANO_DATA_ROOT", str(tmp_path))
    response = request(
        "POST",
        "/api/v1/commands/parse",
        json={
            "schemaVersion": "0.1.0",
            "requestId": str(uuid4()),
            "command": "parse",
            "projectId": "p001",
            "payload": {
                "imageBase64": base64.b64encode(encoded.tobytes()).decode("ascii"),
                "visionMode": "off",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["taskId"]
    result = request("GET", f"/api/v1/projects/p001/parse-results/{task_id}")
    assert result.status_code == 200
    assert result.json()["traceId"] == task_id
    assert result.json()["status"] in ("needsReview", "rejected")


def test_parse_rejects_invalid_base64() -> None:
    response = request(
        "POST",
        "/api/v1/commands/parse",
        json={
            "schemaVersion": "0.1.0",
            "requestId": str(uuid4()),
            "command": "parse",
            "projectId": "p001",
            "payload": {"imageBase64": "%%%", "visionMode": "off"},
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_COMMAND_PAYLOAD"


def test_path_and_body_command_must_match() -> None:
    response = request(
        "POST",
        "/api/v1/commands/layout",
        json={
            "schemaVersion": "0.1.0",
            "requestId": str(uuid4()),
            "command": "parse",
            "projectId": "p001",
            "payload": {},
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "COMMAND_MISMATCH"


def test_scale_calibration_api() -> None:
    response = request(
        "POST",
        "/api/v1/geometry/calibrate-scale",
        json={
            "schemaVersion": "0.1.0",
            "sampleId": "fp-001",
            "observations": [
                {"id": "top-2900", "pixelDistance": 100, "worldDistanceMm": 2900, "confidence": 0.95},
                {"id": "top-2400", "pixelDistance": 81, "worldDistanceMm": 2400, "confidence": 0.95},
                {"id": "top-1500", "pixelDistance": 52, "worldDistanceMm": 1500, "confidence": 0.95},
                {"id": "top-1700", "pixelDistance": 58, "worldDistanceMm": 1700, "confidence": 0.95},
                {"id": "left-3000", "pixelDistance": 104, "worldDistanceMm": 3000, "confidence": 0.95},
                {"id": "left-3200", "pixelDistance": 111, "worldDistanceMm": 3200, "confidence": 0.95},
            ],
            "pixelOrigin": {"x": 200, "y": 369},
            "worldOrigin": {"x": 0, "y": 0},
            "maxRelativeResidual": 0.05,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    assert 28.8 < response.json()["mmPerPixel"] < 29.1


def test_opening_candidates_can_be_reviewed_without_mutating_revision_2(tmp_path, monkeypatch) -> None:
    import json
    import shutil

    from pano_platform import opening_review

    source = opening_review.DATASETS / "fp-001"
    target = tmp_path / "fp-001"
    target.mkdir()
    for name in ("opening-candidates.auto-v2.json", "layout-revision.manual-v2.json"):
        shutil.copy2(source / name, target / name)
    monkeypatch.setattr(opening_review, "DATASETS", tmp_path)

    candidates = request("GET", "/api/v1/projects/fp001/opening-candidates").json()["candidates"]
    decisions = [
        {"candidateId": candidate["id"], "decision": "confirmed" if index in (0, 3, 4, 5, 6) else "rejected"}
        for index, candidate in enumerate(candidates)
    ]
    response = request("POST", "/api/v1/projects/fp001/opening-review", json={"decisions": decisions})

    assert response.status_code == 200
    assert response.json()["confirmedCount"] == 5
    assert json.loads((target / "layout-revision.manual-v2.json").read_text(encoding="utf-8"))["openings"] == []
    reviewed = json.loads((target / "layout-revision.reviewed-v3.json").read_text(encoding="utf-8"))
    assert reviewed["revision"] == 3
    assert len(reviewed["openings"]) == 5
    from pano_platform.contracts import LayoutRevision
    LayoutRevision.model_validate(reviewed)
    audit = json.loads((target / "opening-review.v3.json").read_text(encoding="utf-8"))
    assert len(audit["decisions"]) == 7
    assert audit["outputLayoutHash"] == reviewed["contentHash"]

    changed = [{**decision, "decision": "rejected"} for decision in decisions]
    conflict = request("POST", "/api/v1/projects/fp001/opening-review", json={"decisions": changed})
    assert conflict.status_code == 409


def test_opening_review_rejects_partial_decisions() -> None:
    response = request(
        "POST",
        "/api/v1/projects/fp001/opening-review",
        json={"decisions": [{"candidateId": "opening_candidate_01", "decision": "confirmed"}]},
    )
    assert response.status_code == 409
