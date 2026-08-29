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

import time
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Any

from .provider import canonical_hash


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
    normalizedDraftSha256: str
    rawResponseSha256: str
    evidence: dict
    createdAt: float


class VisionResultCache:
    """Thread-safe in-memory cache keyed by the canonical job key."""

    DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6h
    MAX_ENTRIES = 256

    def __init__(self, ttl_seconds: float | None = None, max_entries: int = MAX_ENTRIES) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else self.DEFAULT_TTL_SECONDS
        self._max = max_entries
        self._store: dict[str, CachedVisionResult] = {}
        self._in_flight: dict[str, Any] = {}
        self._inflight_lock = RLock()
        self._store_lock = Lock()

    def get(self, job_key: str) -> CachedVisionResult | None:
        with self._store_lock:
            entry = self._store.get(job_key)
            if entry is None:
                return None
            if self._ttl >= 0 and time.time() - entry.createdAt > self._ttl:
                self._store.pop(job_key, None)
                return None
            return entry

    def put(self, result: CachedVisionResult) -> None:
        with self._store_lock:
            # simple size bound: evict oldest if over capacity
            if len(self._store) >= self._max and result.jobKey not in self._store:
                oldest = min(self._store, key=lambda k: self._store[k].createdAt)
                self._store.pop(oldest, None)
            self._store[result.jobKey] = result

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
        with self._inflight_lock:
            self._in_flight.clear()

    def size(self) -> int:
        with self._store_lock:
            return len(self._store)
