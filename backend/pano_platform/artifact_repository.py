from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

from .artifact_graph import find_invalidated_artifacts
from .contracts import ArtifactManifest


class ArtifactNotPublishableError(ValueError):
    pass


class StalePublicationError(ValueError):
    pass


@dataclass(frozen=True)
class Publication:
    project_id: str
    manifest_artifact_id: str
    revision: int


class ArtifactRepository:
    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    def register_project(self, project_id: str) -> None:
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO projects(id) VALUES (%s)
                ON CONFLICT (id) DO UPDATE SET updated_at = now()
                """,
                (project_id,),
            )

    def register_artifact(self, artifact: ArtifactManifest, storage_key: str) -> str:
        with self.connection.transaction():
            row = self.connection.execute(
                """
                INSERT INTO artifacts(
                    id, project_id, artifact_type, content_hash, byte_length, content_type,
                    generator_name, generator_version, input_digests, storage_key, metadata, created_at
                ) VALUES (
                    %(id)s, %(project_id)s, %(artifact_type)s, %(content_hash)s, %(byte_length)s,
                    %(content_type)s, %(generator_name)s, %(generator_version)s, %(input_digests)s,
                    %(storage_key)s, %(metadata)s::jsonb, %(created_at)s
                )
                ON CONFLICT (project_id, content_hash) DO NOTHING
                RETURNING id
                """,
                {
                    "id": artifact.id,
                    "project_id": artifact.projectId,
                    "artifact_type": artifact.artifactType.value,
                    "content_hash": artifact.contentHash,
                    "byte_length": artifact.byteLength,
                    "content_type": artifact.contentType,
                    "generator_name": artifact.generator.name,
                    "generator_version": artifact.generator.version,
                    "input_digests": artifact.inputDigests,
                    "storage_key": storage_key,
                    "metadata": psycopg.types.json.Jsonb(artifact.metadata),
                    "created_at": artifact.createdAt,
                },
            ).fetchone()
            if row:
                return row[0]
            existing = self.connection.execute(
                "SELECT id FROM artifacts WHERE project_id = %s AND content_hash = %s",
                (artifact.projectId, artifact.contentHash),
            ).fetchone()
            if not existing:
                raise RuntimeError("artifact registration did not converge")
            return existing[0]

    def publish_manifest(self, project_id: str, artifact_id: str, revision: int) -> Publication:
        with self.connection.transaction():
            artifact = self.connection.execute(
                """
                SELECT artifact_type, status FROM artifacts
                WHERE id = %s AND project_id = %s
                FOR UPDATE
                """,
                (artifact_id, project_id),
            ).fetchone()
            if not artifact or artifact[0] != "panoManifest" or artifact[1] != "ACTIVE":
                raise ArtifactNotPublishableError(artifact_id)

            published = self.connection.execute(
                """
                INSERT INTO project_publications(project_id, manifest_artifact_id, revision)
                VALUES (%s, %s, %s)
                ON CONFLICT (project_id) DO UPDATE SET
                    manifest_artifact_id = EXCLUDED.manifest_artifact_id,
                    revision = EXCLUDED.revision,
                    published_at = now()
                WHERE project_publications.revision < EXCLUDED.revision
                RETURNING project_id, manifest_artifact_id, revision
                """,
                (project_id, artifact_id, revision),
            ).fetchone()
            if published:
                return Publication(*published)

            current = self.connection.execute(
                "SELECT project_id, manifest_artifact_id, revision FROM project_publications WHERE project_id = %s",
                (project_id,),
            ).fetchone()
            if current and current[1] == artifact_id and current[2] == revision:
                return Publication(*current)
            raise StalePublicationError(f"current revision is {current[2] if current else 'missing'}")

    def get_publication(self, project_id: str) -> Publication | None:
        row = self.connection.execute(
            "SELECT project_id, manifest_artifact_id, revision FROM project_publications WHERE project_id = %s",
            (project_id,),
        ).fetchone()
        return Publication(*row) if row else None

    def invalidate_by_digests(self, project_id: str, changed_digests: Iterable[str]) -> list[str]:
        with self.connection.transaction():
            with self.connection.cursor(row_factory=dict_row) as cursor:
                rows = cursor.execute(
                    "SELECT * FROM artifacts WHERE project_id = %s AND status = 'ACTIVE' ORDER BY created_at, id",
                    (project_id,),
                ).fetchall()
            manifests = [self._manifest_from_row(row) for row in rows]
            invalidated = find_invalidated_artifacts(manifests, changed_digests)
            artifact_ids = [artifact.id for artifact in invalidated]
            if not artifact_ids:
                return []
            self.connection.execute(
                """
                UPDATE artifacts SET status = 'INVALIDATED', invalidated_at = now()
                WHERE project_id = %s AND id = ANY(%s)
                """,
                (project_id, artifact_ids),
            )
            self.connection.execute(
                "DELETE FROM project_publications WHERE project_id = %s AND manifest_artifact_id = ANY(%s)",
                (project_id, artifact_ids),
            )
            return artifact_ids

    @staticmethod
    def _manifest_from_row(row: dict) -> ArtifactManifest:
        return ArtifactManifest.model_validate(
            {
                "id": row["id"],
                "projectId": row["project_id"],
                "artifactType": row["artifact_type"],
                "contentHash": row["content_hash"].strip(),
                "byteLength": row["byte_length"],
                "contentType": row["content_type"],
                "generator": {
                    "name": row["generator_name"],
                    "version": row["generator_version"],
                },
                "inputDigests": [digest.strip() for digest in row["input_digests"]],
                "createdAt": row["created_at"],
                "metadata": row["metadata"],
            }
        )
