"""T05: job key, cache idempotency and stale suppression."""

from __future__ import annotations

import time

import pytest

from pano_platform.vision.cache import CachedVisionResult, VisionJobKey, VisionResultCache


def make_key(**overrides) -> VisionJobKey:
    values = {
        "imageSha256": "a" * 64,
        "provider": "tuzi",
        "baseUrlProfile": "https://api.tu-zi.com",
        "requestedModel": "gemini-3.5-flash",
        "promptVersion": "floorplan_parse_v1",
        "draftSchemaVersion": "0.1.0",
        "preprocessingVersion": "v1",
    }
    values.update(overrides)
    return VisionJobKey(**values)


def test_job_key_changes_on_any_component() -> None:
    base = make_key()
    changes = [
        {"imageSha256": "b" * 64},
        {"provider": "other"},
        {"baseUrlProfile": "https://other.example"},
        {"requestedModel": "gemini-3.1-pro"},
        {"promptVersion": "floorplan_parse_v2"},
        {"draftSchemaVersion": "0.2.0"},
        {"preprocessingVersion": "v2"},
    ]
    for change in changes:
        assert make_key(**change).jobKey != base.jobKey


def test_job_key_is_order_independent_and_stable() -> None:
    a = make_key()
    b = make_key()
    assert a.jobKey == b.jobKey
    assert a.jobKey == a.digest()


def test_cache_get_put_roundtrip() -> None:
    cache = VisionResultCache()
    key = make_key()
    result = CachedVisionResult(
        jobKey=key.jobKey,
        normalizedDraftSha256="n" * 64,
        rawResponseSha256="r" * 64,
        evidence={"provider": "tuzi"},
        createdAt=time.time(),
    )
    cache.put(result)
    assert cache.get(key.jobKey) is result


def test_cache_returns_none_after_ttl_expiry() -> None:
    cache = VisionResultCache(ttl_seconds=0.001)
    key = make_key()
    result = CachedVisionResult(
        jobKey=key.jobKey, normalizedDraftSha256="n" * 64,
        rawResponseSha256="r" * 64, evidence={}, createdAt=time.time(),
    )
    cache.put(result)
    assert cache.get(key.jobKey) is not None
    time.sleep(0.01)
    assert cache.get(key.jobKey) is None


def test_idempotency_single_inflight_for_same_key() -> None:
    cache = VisionResultCache()
    key = make_key()
    assert cache.claim_in_flight(key.jobKey) is True
    # a concurrent identical request must NOT claim another slot
    assert cache.claim_in_flight(key.jobKey) is False
    cache.release_in_flight(key.jobKey)
    assert cache.claim_in_flight(key.jobKey) is True


def test_cache_evicts_oldest_beyond_capacity() -> None:
    cache = VisionResultCache(max_entries=2)
    k1 = make_key(imageSha256="1" * 64)
    k2 = make_key(imageSha256="2" * 64)
    k3 = make_key(imageSha256="3" * 64)
    for i, key in enumerate([k1, k2, k3]):
        cache.put(CachedVisionResult(
            jobKey=key.jobKey, normalizedDraftSha256="n" * 64,
            rawResponseSha256="r" * 64, evidence={}, createdAt=time.time() + float(i),
        ))
        cache.release_in_flight(key.jobKey)
    # first (oldest) is evicted, so k1 is gone
    assert cache.get(k1.jobKey) is None
    assert cache.get(k2.jobKey) is not None
    assert cache.get(k3.jobKey) is not None
