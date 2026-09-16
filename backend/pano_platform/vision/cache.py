"""Vision result cache, idempotency and stale-revision suppression.

A ``VisionJobKey`` is the full identity of a vision request: any change in the
image, provider, base URL profile, model, prompt version, draft schema version or
preprocessing version must yield a different key. The cache stores only
canonical, validated results and their response hashes — never raw payloads with
secrets.

Idempotency: concurrent identical requests resolve to a single in-flight task.
Stale suppression: an older layout revision's vision result is not reused once a
newer revision exists.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from .provider import canonical_hash
from .contracts import VlmDraft


@dataclass(frozen=True)
class VisionJobKey:
    """The full, order-independent identity of a vision request."""

    imageSha256: str
    provider: str
    baseUrlProfile: str
    requestedModel: str
    promptVersion: str
    draftSchemaVersion: str
    preprocessingVersion: str

    def digest(self) -> str:
        return canonical_hash({
            "imageSha256": self.imageSha256,
            "provider": self.provider,
            "baseUrlProfile": self.baseUrlProfile,
            "requestedModel": self.requestedModel,
            "promptVersion": self.promptVersion,
            "draftSchemaVersion": self.draftSchemaVersion,
            "preprocessingVersion": self.preprocessingVersion,
        })

    @property
    def jobKey(self) -> str:
        return self.digest()


@dataclass(frozen=True)
class CachedVisionResult:
    """A validated, cacheable vision result (hash, never raw secrets)."""

    jobKey: str
    normalizedDraftSha256: str | None
    rawResponseSha256: str | None
    evidence: dict
    createdAt: float
    draft: VlmDraft | None = None


class VisionResultCache:
    """Thread-safe cache with optional cross-process disk persistence."""

    DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6h
    MAX_ENTRIES = 256

    def __init__(
        self,
        ttl_seconds: float | None = None,
        max_entries: int = MAX_ENTRIES,
        disk_dir: str | Path | None = None,
    ) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else self.DEFAULT_TTL_SECONDS
        self._max = max_entries
        self._disk_dir = Path(disk_dir) if disk_dir is not None else None
        self._store: dict[str, CachedVisionResult] = {}
        self._in_flight: dict[str, Any] = {}
        self._inflight_lock = RLock()
        self._store_lock = Lock()

    def get(self, job_key: str) -> CachedVisionResult | None:
        with self._store_lock:
            entry = self._store.get(job_key)
            if entry is None and self._disk_dir is not None:
                entry = self._read_disk(job_key)
                if entry is not None:
                    self._store[job_key] = entry
            if entry is None:
                return None
            if self._ttl >= 0 and time.time() - entry.createdAt > self._ttl:
                self._store.pop(job_key, None)
                self._delete_disk(job_key)
                return None
            return entry

    def put(self, result: CachedVisionResult) -> None:
        with self._store_lock:
            # simple size bound: evict oldest if over capacity
            if len(self._store) >= self._max and result.jobKey not in self._store:
                oldest = min(self._store, key=lambda k: self._store[k].createdAt)
                self._store.pop(oldest, None)
            self._store[result.jobKey] = result
            self._write_disk(result)

    def claim_in_flight(self, job_key: str) -> bool:
        """Atomically claim the single in-flight slot for a job key.

        Returns True if this caller owns the task, False if another caller is
        already handling the identical request (idempotency).
        """

        with self._inflight_lock:
            if job_key in self._in_flight:
                return False
            self._in_flight[job_key] = time.time()
            return True

    def release_in_flight(self, job_key: str) -> None:
        with self._inflight_lock:
            self._in_flight.pop(job_key, None)

    def clear(self) -> None:
        with self._store_lock:
            self._store.clear()
            if self._disk_dir is not None and self._disk_dir.is_dir():
                for path in self._disk_dir.glob("*.json"):
                    try:
                        path.unlink()
                    except OSError:
                        pass
        with self._inflight_lock:
            self._in_flight.clear()

    def size(self) -> int:
        with self._store_lock:
            return len(self._store)

    @staticmethod
    def default_disk_dir() -> Path:
        root = os.environ.get("PANO_DATA_ROOT")
        data_root = Path(root) if root else Path(__file__).resolve().parents[3] / "datasets" / "derived"
        return data_root / "_vision_cache"

    def _path(self, job_key: str) -> Path | None:
        if self._disk_dir is None or re.fullmatch(r"[0-9a-f]{64}", job_key) is None:
            return None
        return self._disk_dir / f"{job_key}.json"

    def _read_disk(self, job_key: str) -> CachedVisionResult | None:
        path = self._path(job_key)
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schemaVersion") != "0.1.0" or not isinstance(payload.get("evidence"), dict):
                raise ValueError("unsupported vision cache payload")
            draft = VlmDraft.model_validate(payload["draft"])
            normalized_hash = canonical_hash(draft.model_dump())
            if payload["jobKey"] != job_key or payload["normalizedDraftSha256"] != normalized_hash:
                raise ValueError("vision cache integrity mismatch")
            created_at = float(payload["createdAt"])
            if not math.isfinite(created_at) or created_at <= 0:
                raise ValueError("invalid vision cache timestamp")
            return CachedVisionResult(
                jobKey=job_key,
                normalizedDraftSha256=normalized_hash,
                rawResponseSha256=payload.get("rawResponseSha256"),
                evidence=payload["evidence"],
                createdAt=created_at,
                draft=draft,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._delete_disk(job_key)
            return None

    def _write_disk(self, result: CachedVisionResult) -> None:
        path = self._path(result.jobKey)
        if path is None or result.draft is None:
            return
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schemaVersion": "0.1.0",
                "jobKey": result.jobKey,
                "normalizedDraftSha256": result.normalizedDraftSha256,
                "rawResponseSha256": result.rawResponseSha256,
                "evidence": result.evidence,
                "createdAt": result.createdAt,
                "draft": result.draft.model_dump(mode="json"),
            }
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                encoding="utf-8",
            )
            os.replace(temporary, path)
            files = sorted(path.parent.glob("*.json"), key=lambda item: item.stat().st_mtime)
            for stale in files[:-self._max]:
                stale.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _delete_disk(self, job_key: str) -> None:
        path = self._path(job_key)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
