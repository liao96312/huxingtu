from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID
from uuid import uuid4

from pydantic import BaseModel

from .contracts import AcceptedTask, CommandEnvelope, CommandHandler, CommandName


class CommandNotImplementedError(LookupError):
    pass


class CommandFacade:
    def __init__(self) -> None:
        self._handlers: dict[CommandName, CommandHandler] = {}

    def register(self, command: CommandName, handler: CommandHandler) -> None:
        self._handlers[command] = handler

    async def dispatch(self, envelope: CommandEnvelope) -> AcceptedTask:
        handler = self._handlers.get(envelope.command)
        if handler is None:
            raise CommandNotImplementedError(envelope.command.value)
        return await handler(envelope)

    def register_parse(self, handler: CommandHandler) -> None:
        self.register(CommandName.PARSE, handler)


async def accept_for_contract_test(_: CommandEnvelope) -> AcceptedTask:
    """Minimal handler used by smoke checks until the first real stage worker lands."""
    return AcceptedTask(taskId=uuid4())


async def parse_command(envelope: CommandEnvelope) -> AcceptedTask:
    """Frozen parse command handler: runs the T08 pipeline in ASSISTED mode.

    The pipeline is conservative: it never publishes a wrong ``LayoutRevision``.
    Vision is attempted via the configured ``VisionConfig`` if present; otherwise
    the run falls back to CV-only and records ``NEEDS_REVIEW``.
    """

    from .floorplan.parse import VisionMode, orchestrate

    image_bytes = _decode_image(envelope.payload)
    mode = VisionMode(envelope.payload.get("visionMode", VisionMode.ASSISTED.value))
    vision_config = None
    if mode is not VisionMode.OFF and envelope.payload.get("allowExternalVision") is True:
        try:
            from .vision.config import VisionConfig

            vision_config = VisionConfig.from_env()
        except ValueError:
            # Missing provider configuration is a supported CV-only fallback.
            pass

    run = await asyncio.to_thread(
        orchestrate,
        image_bytes,
        vision_mode=mode,
        vision_config=vision_config,
        project_id=envelope.projectId,
    )
    task_id = UUID(run.traceId)
    _write_parse_result(envelope.projectId, task_id, run)
    return AcceptedTask(taskId=task_id)


def load_parse_result(project_id: str, task_id: UUID) -> dict:
    path = _parse_result_root() / project_id / "parse" / f"{task_id}.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _decode_image(payload: dict) -> bytes:
    direct = payload.get("imageBytes")
    if isinstance(direct, bytes):
        return direct
    encoded = payload.get("imageBase64")
    if not isinstance(encoded, str) or not encoded:
        return b""
    if encoded.startswith("data:"):
        try:
            encoded = encoded.split(",", 1)[1]
        except IndexError as error:
            raise ValueError("invalid image data URI") from error
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise ValueError("invalid imageBase64") from error


def _parse_result_root() -> Path:
    configured = os.environ.get("PANO_DATA_ROOT")
    return Path(configured) if configured else Path(__file__).resolve().parents[2] / "datasets" / "derived"


def _write_parse_result(project_id: str, task_id: UUID, run: Any) -> None:
    folder = _parse_result_root() / project_id / "parse"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{task_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(_jsonable(run), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
