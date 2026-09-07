"""Run the automatic upload chain against locally prepared ResPlan truth.

This intentionally reports count deltas instead of a release F1: the ResPlan
images are synthetic rasters generated from vector truth, not real customer
floorplan scans.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pano_platform.pipeline import run_upload_pipeline  # noqa: E402

DEFAULT_CORPUS = ROOT / "tmp" / "external" / "resplan" / "derived"
DEFAULT_ARTIFACTS = ROOT / "tmp" / "external" / "resplan" / "pipeline"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the upload chain on prepared ResPlan truth")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    manifest = json.loads((args.corpus / "manifest.json").read_text(encoding="utf-8"))
    args.artifacts.mkdir(parents=True, exist_ok=True)
    os.environ["PANO_DATA_ROOT"] = str(args.artifacts)

    samples = []
    for item in manifest["samples"][: args.limit]:
        sample_id = item["sampleId"]
        truth = json.loads((args.corpus / f"{sample_id}.truth.json").read_text(encoding="utf-8"))
        result = run_upload_pipeline(
            (args.corpus / f"{sample_id}.png").read_bytes(),
            project_id=sample_id,
            allow_external_vision=False,
            generate_ai=False,
        )
        truth_doors = sum(item["type"] == "door" for item in truth["openings"])
        truth_windows = sum(item["type"] == "window" for item in truth["openings"])
        predicted_doors = result.get("automaticCvDoorCount", 0)
        predicted_windows = result.get("automaticCvWindowCount", 0)
        predicted_openings = predicted_doors + predicted_windows
        samples.append({
            "sampleId": sample_id,
            "pipelineStatus": result.get("pipelineStatus"),
            "viewerReady": bool(result.get("viewerUrl")),
            "truthRooms": len(truth["rooms"]),
            "predictedRooms": result.get("roomCount", 0),
            "roomCountDelta": result.get("roomCount", 0) - len(truth["rooms"]),
            "truthOpenings": len(truth["openings"]),
            "predictedOpenings": predicted_openings,
            "openingCountDelta": predicted_openings - len(truth["openings"]),
            "truthDoors": truth_doors,
            "predictedDoors": predicted_doors,
            "doorCountDelta": predicted_doors - truth_doors,
            "truthWindows": truth_windows,
            "predictedWindows": predicted_windows,
            "windowCountDelta": predicted_windows - truth_windows,
        })

    totals = {
        key: sum(item[key] for item in samples)
        for key in (
            "truthRooms", "predictedRooms", "truthOpenings", "predictedOpenings",
            "truthDoors", "predictedDoors", "truthWindows", "predictedWindows",
        )
    }
    report = {
        "dataset": manifest["dataset"],
        "sourceVersion": manifest["sourceVersion"],
        "converterVersion": manifest.get("converterVersion"),
        "note": "Count regression only; synthetic ResPlan rasters are not real-image accuracy evidence.",
        "automaticChainReady": all(item["viewerReady"] for item in samples),
        "totals": totals,
        "samples": samples,
    }
    output = args.out or args.corpus / "pipeline-regression.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["automaticChainReady"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
