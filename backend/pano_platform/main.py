from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import re
import json
import hashlib
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path as FilePath
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Path, UploadFile, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .contracts import (
    CommandEnvelope,
    CommandName,
    ErrorEnvelope,
    FurniturePlan,
    OpeningCandidateSet,
    OpeningReviewRequest,
    OpeningReviewResult,
    SceneGeometry,
    SCHEMA_VERSION,
    ScaleCalibrationRequest,
    ScaleCalibrationResult,
    PanoManifest,
    ShareCreateRequest,
)
from .facade import CommandFacade, CommandNotImplementedError, load_parse_result, parse_command
from .geometry import calibrate_scale
from .furniture import build_furniture_plan, load_furniture_inputs
from .opening_review import complete_opening_review, load_derived_rooms, load_latest_reviewed_layout, load_opening_candidates
from .pipeline import load_preview_scene_geometry, load_style_catalog, load_style_context, run_upload_pipeline
from .scene_geometry import build_scene_geometry

app = FastAPI(title="Pano Platform API", version=__version__)
artifact_root = FilePath(os.environ.get(
    "PANO_DATA_ROOT", FilePath(__file__).resolve().parents[2] / "datasets" / "derived"
))
artifact_root.mkdir(parents=True, exist_ok=True)
app.mount("/artifacts", StaticFiles(directory=artifact_root), name="artifacts")
facade = CommandFacade()
facade.register_parse(parse_command)
_pipeline_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pano-pipeline")
_pipeline_tasks: dict[UUID, dict] = {}
_pipeline_state_lock = threading.RLock()
_pipeline_active: set[UUID] = set()


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
        try:
            return load_preview_scene_geometry(project_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="scene geometry not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/v1/projects/{project_id}/furniture-plan", response_model=FurniturePlan)
async def get_furniture_plan(project_id: str = Path(pattern=r"^[a-zA-Z0-9_-]+$")) -> FurniturePlan:
    try:
        layout, rooms, cameras = load_furniture_inputs(project_id)
        return build_furniture_plan(layout, rooms, cameras)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="furniture inputs not found") from error


@app.post("/api/v1/pipeline/upload")
async def upload_floorplan_pipeline(
    file: UploadFile = File(...),
    projectId: str | None = Form(default=None),
    allowExternalVision: bool = Form(default=True),
    generateAi: bool = Form(default=True),
    styleId: str = Form(default="modern"),
) -> dict:
    if file.content_type not in ("image/png", "image/jpeg", "image/webp"):
        raise HTTPException(status_code=415, detail="only PNG/JPEG/WebP floorplans are supported")
    image_bytes = await file.read(10 * 1024 * 1024 + 1)
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="floorplan exceeds 10MB")
    project_id = projectId or f"upload-{uuid4().hex[:10]}"
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", project_id):
        raise HTTPException(status_code=422, detail="invalid projectId")
    try:
        load_style_context(styleId)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        task_id = uuid4()
        task = {
            "taskId": str(task_id), "projectId": project_id,
            "status": "PROCESSING", "progress": 0, "stage": "queued",
            "allowExternalVision": allowExternalVision, "generateAi": generateAi,
            "styleId": styleId, "result": None, "error": None,
        }
        _persist_pipeline_task(task_id, task, image_bytes)
        with _pipeline_state_lock:
            _pipeline_tasks[task_id] = task
        _submit_pipeline_task(task_id, task)
        return JSONResponse(status_code=202, content={
            "taskId": str(task_id), "projectId": project_id,
            "status": "PROCESSING", "progress": 0,
            "pollUrl": f"/api/v1/pipeline/tasks/{task_id}",
        })
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/pipeline/styles")
async def get_pipeline_styles() -> dict:
    return {
        "styles": [
            {"id": item.id, "label": item.label}
            for item in load_style_catalog().styles
        ]
    }


@app.post("/api/v1/projects/{project_id}/shares")
async def create_share(
    request: ShareCreateRequest,
    project_id: str = Path(pattern=r"^[a-zA-Z0-9_-]{1,128}$"),
) -> dict:
    manifest_path = _pipeline_manifest_path(project_id)
    if not manifest_path.is_file():
        raise HTTPException(status_code=404, detail="pano manifest not found")
    try:
        manifest = PanoManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=409, detail="pano manifest is invalid") from error

    token = secrets.token_urlsafe(32)
    share_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=request.ttlSeconds)
    grant = {
        "shareId": str(share_id), "projectId": project_id,
        "tokenHash": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "manifestRevision": manifest.revision, "status": "ACTIVE",
        "retentionMode": request.retentionMode,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "expiresAt": expires_at.isoformat(),
    }
    _persist_share_grant(share_id, grant)
    base_url = os.environ.get("PANO_PUBLIC_BASE_URL", "").rstrip("/")
    share_url = f"{base_url}/?shareToken={token}" if base_url else f"/?shareToken={token}"
    return {
        "shareId": str(share_id), "shareUrl": share_url,
        "expiresAt": grant["expiresAt"], "manifestRevision": manifest.revision,
        "retentionMode": request.retentionMode,
    }


@app.get("/api/v1/shares/{token}")
async def read_share(token: str) -> dict:
    grant = _find_share_grant(token)
    if grant is None:
        raise HTTPException(status_code=404, detail="share not found")
    if grant.get("status") != "ACTIVE":
        raise HTTPException(status_code=410, detail="share is no longer available")
    try:
        expires_at = datetime.fromisoformat(grant["expiresAt"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=410, detail="share is no longer available")
    if datetime.now(timezone.utc) >= expires_at:
        grant["status"] = "EXPIRED"
        _persist_share_grant(UUID(grant["shareId"]), grant)
        raise HTTPException(status_code=410, detail="share is no longer available")
    manifest_path = _pipeline_manifest_path(grant["projectId"])
    if not manifest_path.is_file():
        raise HTTPException(status_code=410, detail="share is no longer available")
    try:
        manifest = PanoManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=410, detail="share is no longer available") from error
    return manifest.model_dump(mode="json")


@app.delete("/api/v1/projects/{project_id}/shares/{share_id}")
async def revoke_share(
    project_id: str = Path(pattern=r"^[a-zA-Z0-9_-]{1,128}$"),
    share_id: UUID = Path(),
) -> dict:
    grant = _load_share_grant(share_id)
    if grant is None or grant.get("projectId") != project_id:
        raise HTTPException(status_code=404, detail="share not found")
    if grant.get("status") == "ACTIVE":
        grant["status"] = "REVOKED"
        _persist_share_grant(share_id, grant)
    return {"shareId": str(share_id), "status": grant.get("status", "REVOKED")}


def _run_pipeline_task(
    task_id: UUID, image_bytes: bytes, project_id: str,
    allow_external: bool, generate_ai: bool, style_id: str,
) -> None:
    try:
        def report(progress: int, stage: str) -> None:
            _update_pipeline_task(task_id, progress=progress, stage=stage)

        result = run_upload_pipeline(
            image_bytes, project_id=project_id,
            allow_external_vision=allow_external, generate_ai=generate_ai,
            style_id=style_id, progress_callback=report,
        )
        status_value = "DONE" if result.get("viewerUrl") else "NEEDS_REVIEW"
        _update_pipeline_task(task_id, **{
            "taskId": str(task_id), "projectId": project_id,
            "status": status_value, "progress": 100,
            "stage": "done" if status_value == "DONE" else "needs-review",
            "result": result, "error": None,
        })
    except Exception as error:
        _update_pipeline_task(task_id, **{
            "taskId": str(task_id), "projectId": project_id,
            "status": "FAILED", "progress": 100,
            "stage": "failed",
            "result": None, "error": str(error)[:500],
        })
    finally:
        with _pipeline_state_lock:
            _pipeline_active.discard(task_id)


@app.get("/api/v1/pipeline/tasks/{task_id}")
async def get_pipeline_task(task_id: UUID) -> dict:
    with _pipeline_state_lock:
        task = _pipeline_tasks.get(task_id) or _load_pipeline_task(task_id)
        if task is not None:
            _pipeline_tasks[task_id] = task
    if task is None:
        raise HTTPException(status_code=404, detail="pipeline task not found")
    if task.get("status") == "PROCESSING":
        _submit_pipeline_task(task_id, task)
    return task


def _pipeline_task_root() -> FilePath:
    return FilePath(os.environ.get("PANO_DATA_ROOT", artifact_root)) / "_pipeline_tasks"


def _pipeline_manifest_path(project_id: str) -> FilePath:
    return FilePath(os.environ.get("PANO_DATA_ROOT", artifact_root)) / project_id / "pano-manifest.json"


def _share_root() -> FilePath:
    return FilePath(os.environ.get("PANO_DATA_ROOT", artifact_root)) / "_shares"


def _share_grant_path(share_id: UUID) -> FilePath:
    return _share_root() / f"{share_id}.json"


def _persist_share_grant(share_id: UUID, grant: dict) -> None:
    root = _share_root()
    root.mkdir(parents=True, exist_ok=True)
    target = _share_grant_path(share_id)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(grant, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)


def _load_share_grant(share_id: UUID) -> dict | None:
    path = _share_grant_path(share_id)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _find_share_grant(token: str) -> dict | None:
    if len(token) < 32 or len(token) > 128:
        return None
    digest = hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()
    for path in _share_root().glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and secrets.compare_digest(value.get("tokenHash", ""), digest):
            return value
    return None


def _pipeline_task_dir(task_id: UUID) -> FilePath:
    return _pipeline_task_root() / str(task_id)


def _persist_pipeline_task(task_id: UUID, task: dict, image_bytes: bytes | None = None) -> None:
    folder = _pipeline_task_dir(task_id)
    folder.mkdir(parents=True, exist_ok=True)
    if image_bytes is not None:
        input_path = folder / "input.bin"
        temporary_input = input_path.with_suffix(".bin.tmp")
        temporary_input.write_bytes(image_bytes)
        temporary_input.replace(input_path)
    target = folder / "task.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(task, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)


def _load_pipeline_task(task_id: UUID) -> dict | None:
    path = _pipeline_task_dir(task_id) / "task.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _update_pipeline_task(task_id: UUID, **changes) -> None:
    with _pipeline_state_lock:
        task = _pipeline_tasks.get(task_id) or _load_pipeline_task(task_id) or {"taskId": str(task_id)}
        task.update(changes)
        _pipeline_tasks[task_id] = task
        _persist_pipeline_task(task_id, task)


def _submit_pipeline_task(task_id: UUID, task: dict) -> None:
    with _pipeline_state_lock:
        if task_id in _pipeline_active:
            return
        input_path = _pipeline_task_dir(task_id) / "input.bin"
        if not input_path.is_file():
            _update_pipeline_task(task_id, status="FAILED", progress=100, stage="failed", result=None, error="persisted task input missing")
            return
        _pipeline_active.add(task_id)
    try:
        image_bytes = input_path.read_bytes()
        _pipeline_executor.submit(
            _run_pipeline_task, task_id, image_bytes, task["projectId"],
            bool(task.get("allowExternalVision", True)), bool(task.get("generateAi", True)),
            task.get("styleId", "modern"),
        )
    except Exception as error:
        with _pipeline_state_lock:
            _pipeline_active.discard(task_id)
        _update_pipeline_task(
            task_id, status="FAILED", progress=100, stage="failed",
            result=None, error=f"persisted task resume failed: {error}"[:500],
        )


@app.on_event("startup")
async def recover_pipeline_tasks() -> None:
    root = _pipeline_task_root()
    if not root.is_dir():
        return
    for path in root.glob("*/task.json"):
        try:
            task_id = UUID(path.parent.name)
        except ValueError:
            continue
        task = _load_pipeline_task(task_id)
        if task and task.get("status") == "PROCESSING":
            with _pipeline_state_lock:
                _pipeline_tasks[task_id] = task
            _submit_pipeline_task(task_id, task)


@app.get("/api/v1/projects/{project_id}/parse-results/{task_id}")
async def get_parse_result(
    project_id: str = Path(pattern=r"^[a-zA-Z0-9_-]+$"), task_id: UUID = Path()
) -> dict:
    try:
        return load_parse_result(project_id, task_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="parse result not found") from error


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
    except ValueError as error:
        detail = ErrorEnvelope(
            code="INVALID_COMMAND_PAYLOAD",
            message=str(error),
            traceId=trace_id,
        )
        return JSONResponse(status_code=422, content=detail.model_dump(mode="json"))
    except CommandNotImplementedError:
        error = ErrorEnvelope(
            code="COMMAND_NOT_IMPLEMENTED",
            message=f"command {command.value} has no registered handler",
            traceId=trace_id,
        )
        return JSONResponse(status_code=501, content=error.model_dump(mode="json"))
    return JSONResponse(status_code=202, content=task.model_dump(mode="json"))
