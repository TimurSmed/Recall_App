"""Pytest fixtures using psycopg and a dedicated PostgreSQL test database."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterator

import psycopg
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

load_dotenv()

ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT / "sql_migrations"


def _connection_kwargs(database_name: str) -> dict:
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    if not user or not password:
        raise RuntimeError("POSTGRES_USER and POSTGRES_PASSWORD must be set in .env")

    return {
        "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": database_name,
        "user": user,
        "password": password,
        "row_factory": dict_row,
    }


def _sql_statements(sql: str) -> list[str]:
    # These numbered migrations contain plain DDL and no semicolons inside strings.
    return [part.strip() for part in sql.split(";") if part.strip()]


@pytest.fixture(scope="session")
def test_database() -> dict:
    """Rebuild schema only in POSTGRES_TEST_DB, never in the application database."""
    test_db_name = os.getenv("POSTGRES_TEST_DB")
    if not test_db_name:
        raise RuntimeError("POSTGRES_TEST_DB is missing from .env")

    production_db_name = os.getenv("POSTGRES_DB", "recall")
    if test_db_name == production_db_name:
        raise RuntimeError(
            "Safety stop: POSTGRES_TEST_DB equals POSTGRES_DB. "
            "Tests require a separate database so production data cannot be erased."
        )

    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migrations:
        raise RuntimeError(f"No SQL migrations found in {MIGRATIONS_DIR}")

    kwargs = _connection_kwargs(test_db_name)
    with psycopg.connect(**kwargs) as conn:
        with conn.transaction():
            # Reset only the dedicated test database's application tables.
            # Do not run this fixture against POSTGRES_DB.
            for table in (
                "note_relations",
                "note_tags",
                "notes",
                "tags",
                "schema_migrations",
                "alembic_version",
            ):
                conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')

            for migration in migrations:
                sql = migration.read_text(encoding="utf-8-sig")
                for statement in _sql_statements(sql):
                    conn.execute(statement)

            conn.execute(
                """
                CREATE TABLE schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            for migration in migrations:
                digest = hashlib.sha256(
                    migration.read_text(encoding="utf-8-sig").encode("utf-8")
                ).hexdigest()
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (migration.name, digest),
                )

    return kwargs


@pytest.fixture
def client(test_database: dict) -> Iterator[TestClient]:
    """Give each test an empty schema and route requests to the test DB."""
    with psycopg.connect(**test_database) as conn:
        conn.execute(
            "TRUNCATE TABLE note_relations, note_tags, notes, tags "
            "RESTART IDENTITY CASCADE"
        )

    from app.database import get_db
    from app.main import app

    def override_get_db() -> Iterator[psycopg.Connection]:
        conn = psycopg.connect(**test_database)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
