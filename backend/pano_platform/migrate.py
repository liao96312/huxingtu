from __future__ import annotations

import os
from importlib.resources import files

import psycopg


def apply_migrations(database_url: str) -> None:
    migration_root = files("pano_platform").joinpath("migrations")
    migration_files = sorted(item for item in migration_root.iterdir() if item.name.endswith(".sql"))
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        for migration in migration_files:
            if connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = %s",
                (migration.name,),
            ).fetchone():
                continue
            with connection.transaction():
                connection.execute(migration.read_text(encoding="utf-8"), prepare=False)
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (%s)",
                    (migration.name,),
                )


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    apply_migrations(database_url)


if __name__ == "__main__":
    main()
