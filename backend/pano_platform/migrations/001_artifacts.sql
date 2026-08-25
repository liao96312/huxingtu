CREATE TABLE IF NOT EXISTS projects (
    id text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS artifacts (
    id text PRIMARY KEY,
    project_id text NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_type text NOT NULL CHECK (artifact_type IN (
        'datasetManifest',
        'layoutRevision',
        'layoutSolution',
        'cameraPlan',
        'sceneManifest',
        'panoramaReference',
        'panoramaResult',
        'panoManifest'
    )),
    content_hash char(64) NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    byte_length bigint NOT NULL CHECK (byte_length >= 0),
    content_type text NOT NULL,
    generator_name text NOT NULL,
    generator_version text NOT NULL,
    input_digests char(64)[] NOT NULL DEFAULT '{}',
    storage_key text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'INVALIDATED')),
    created_at timestamptz NOT NULL,
    invalidated_at timestamptz,
    UNIQUE (project_id, content_hash)
);

CREATE INDEX IF NOT EXISTS artifacts_project_status_idx ON artifacts(project_id, status);
CREATE INDEX IF NOT EXISTS artifacts_input_digests_idx ON artifacts USING gin(input_digests);

CREATE TABLE IF NOT EXISTS project_publications (
    project_id text PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    manifest_artifact_id text NOT NULL REFERENCES artifacts(id),
    revision bigint NOT NULL CHECK (revision >= 1),
    published_at timestamptz NOT NULL DEFAULT now()
);
