from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, HTTPException, Path, status
from fastapi.responses import JSONResponse

from . import __version__
from .contracts import (
    CommandEnvelope,
    CommandName,
    ErrorEnvelope,
    OpeningCandidateSet,
    OpeningReviewRequest,
    OpeningReviewResult,
    SceneGeometry,
    SCHEMA_VERSION,
    ScaleCalibrationRequest,
    ScaleCalibrationResult,
)
from .facade import CommandFacade, CommandNotImplementedError
from .geometry import calibrate_scale
from .opening_review import complete_opening_review, load_derived_rooms, load_latest_reviewed_layout, load_opening_candidates
from .scene_geometry import build_scene_geometry

app = FastAPI(title="Pano Platform API", version=__version__)
facade = CommandFacade()


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "schemaVersion": SCHEMA_VERSION}


@app.post("/api/v1/geometry/calibrate-scale", response_model=ScaleCalibrationResult)
async def calibrate_floorplan_scale(request: ScaleCalibrationRequest) -> ScaleCalibrationResult:
    return calibrate_scale(request)


@app.get("/api/v1/projects/{project_id}/opening-candidates", response_model=OpeningCandidateSet)
async def get_opening_candidates(project_id: str = Path(pattern=r"^[a-zA-Z0-9_-]+$")) -> OpeningCandidateSet:
    try:
        return load_opening_candidates(project_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="opening candidates not found") from error


@app.post("/api/v1/projects/{project_id}/opening-review", response_model=OpeningReviewResult)
async def review_openings(
    request: OpeningReviewRequest,
    project_id: str = Path(pattern=r"^[a-zA-Z0-9_-]+$"),
) -> OpeningReviewResult:
    try:
        return complete_opening_review(project_id, request)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="opening candidates not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/v1/projects/{project_id}/scene-geometry", response_model=SceneGeometry)
async def get_scene_geometry(project_id: str = Path(pattern=r"^[a-zA-Z0-9_-]+$")) -> SceneGeometry:
    try:
        return build_scene_geometry(load_latest_reviewed_layout(project_id), load_derived_rooms(project_id))
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="reviewed layout not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post(
    "/api/v1/commands/{command}",
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"model": ErrorEnvelope}, 501: {"model": ErrorEnvelope}},
)
async def dispatch_command(
    envelope: CommandEnvelope,
    command: CommandName = Path(),
) -> JSONResponse:
    trace_id = str(uuid4())
    if envelope.command is not command:
        error = ErrorEnvelope(
            code="COMMAND_MISMATCH",
            message="path command and body command must match",
            traceId=trace_id,
            details={"path": command.value, "body": envelope.command.value},
        )
        return JSONResponse(status_code=409, content=error.model_dump(mode="json"))

    try:
        task = await facade.dispatch(envelope)
    except CommandNotImplementedError:
        error = ErrorEnvelope(
            code="COMMAND_NOT_IMPLEMENTED",
            message=f"command {command.value} has no registered handler",
            traceId=trace_id,
        )
        return JSONResponse(status_code=501, content=error.model_dump(mode="json"))
    return JSONResponse(status_code=202, content=task.model_dump(mode="json"))
