import os
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest

from pano_platform.artifact_repository import ArtifactRepository, StalePublicationError
from pano_platform.contracts import ArtifactManifest, ArtifactType, GeneratorRef
from pano_platform.migrate import apply_migrations

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not set")


def artifact(
    project_id: str,
    identifier: str,
    artifact_type: ArtifactType,
    digest_character: str,
    input_characters: list[str],
) -> ArtifactManifest:
    return ArtifactManifest(
        id=identifier,
        projectId=project_id,
        artifactType=artifact_type,
        contentHash=digest_character * 64,
        byteLength=1,
        contentType="application/json",
        generator=GeneratorRef(name="integration-test", version="0.1.0"),
        inputDigests=[character * 64 for character in input_characters],
        createdAt=datetime.now(timezone.utc),
    )


def test_register_publish_invalidate_and_republish() -> None:
    assert DATABASE_URL
    apply_migrations(DATABASE_URL)
    apply_migrations(DATABASE_URL)
    project_id = f"project-{uuid4()}"
    with psycopg.connect(DATABASE_URL) as connection:
        repository = ArtifactRepository(connection)
        repository.register_project(project_id)
        chain = [
            artifact(project_id, f"layout-{uuid4()}", ArtifactType.LAYOUT_SOLUTION, "b", ["a"]),
            artifact(project_id, f"scene-{uuid4()}", ArtifactType.SCENE_MANIFEST, "c", ["b"]),
            artifact(project_id, f"result-{uuid4()}", ArtifactType.PANORAMA_RESULT, "d", ["c"]),
            artifact(project_id, f"manifest-{uuid4()}", ArtifactType.PANO_MANIFEST, "e", ["d"]),
        ]
        for item in chain:
            assert repository.register_artifact(item, f"projects/{project_id}/{item.id}.json") == item.id
            assert repository.register_artifact(item, f"projects/{project_id}/{item.id}.json") == item.id

        publication = repository.publish_manifest(project_id, chain[-1].id, 1)
        assert publication.manifest_artifact_id == chain[-1].id

        invalidated = repository.invalidate_by_digests(project_id, ["a" * 64])
        assert invalidated == [item.id for item in chain]
        assert repository.get_publication(project_id) is None

        next_manifest = artifact(
            project_id,
            f"manifest-{uuid4()}",
            ArtifactType.PANO_MANIFEST,
            "f",
            ["9"],
        )
        repository.register_artifact(next_manifest, f"projects/{project_id}/{next_manifest.id}.json")
        repository.publish_manifest(project_id, next_manifest.id, 2)
        with pytest.raises(StalePublicationError):
            repository.publish_manifest(project_id, next_manifest.id, 1)
        connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        connection.commit()
