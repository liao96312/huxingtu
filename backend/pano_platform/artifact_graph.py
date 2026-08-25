from __future__ import annotations

from collections.abc import Iterable

from .contracts import ArtifactManifest


def find_invalidated_artifacts(
    artifacts: Iterable[ArtifactManifest],
    changed_digests: Iterable[str],
) -> list[ArtifactManifest]:
    """Return transitive dependants in invalidation order."""
    pending_digests = set(changed_digests)
    remaining = list(artifacts)
    invalidated: list[ArtifactManifest] = []

    while True:
        matched = [artifact for artifact in remaining if pending_digests.intersection(artifact.inputDigests)]
        if not matched:
            return invalidated
        for artifact in matched:
            remaining.remove(artifact)
            invalidated.append(artifact)
            pending_digests.add(artifact.contentHash)
