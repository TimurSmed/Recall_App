"""Pytest fixtures for a dedicated multi-tenant PostgreSQL test database."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterator

import psycopg
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

load_dotenv()
os.environ["RECALL_TESTING"] = "1"

ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT / "sql_migrations"


def _admin_kwargs(database_name: str) -> dict:
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


def _sql_statements(text: str) -> list[str]:
    return [part.strip() for part in text.split(";") if part.strip()]


@pytest.fixture(scope="session")
def test_database() -> dict:
    db_name = os.getenv("POSTGRES_TEST_DB")
    if not db_name:
        raise RuntimeError("POSTGRES_TEST_DB is missing from .env")
    if db_name == os.getenv("POSTGRES_DB", "recall"):
        raise RuntimeError("Safety stop: POSTGRES_TEST_DB must differ from POSTGRES_DB")

    app_role = os.getenv("POSTGRES_APP_USER", "recall_app")
    admin_kwargs = _admin_kwargs(db_name)
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    auth_migrations = [m for m in migrations if m.name in {"006_auth_recovery.sql", "007_oauth_accounts.sql"}]
    base_migrations = [m for m in migrations if m.name not in {"006_auth_recovery.sql", "007_oauth_accounts.sql"}]
    if not migrations:
        raise RuntimeError(f"No SQL migrations found in {MIGRATIONS_DIR}")

    from app.multitenant import apply_multitenant_schema

    fake_hash = "pbkdf2_sha256$600000$ZmFrZXNhbHQ$ZmFrZWRpZ2VzdA"
    with psycopg.connect(**admin_kwargs) as conn:
        with conn.transaction():
            for table in ("note_relations", "note_tags", "notes", "tags", "users", "schema_migrations"):
                conn.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table)))
            for migration in base_migrations:
                text = migration.read_text(encoding="utf-8-sig")
                for statement in _sql_statements(text):
                    conn.execute(statement)
            conn.execute(
                """CREATE TABLE schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )"""
            )
            for migration in base_migrations:
                text = migration.read_text(encoding="utf-8-sig")
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (migration.name, digest),
                )
            owner_id = apply_multitenant_schema(
                conn,
                owner_username="test_owner",
                owner_password_hash=fake_hash,
                owner_email="test-owner@example.invalid",
            )

            # The auth migration depends on the users table created by the
            # multi-tenant bootstrap, so apply it after that schema helper.
            for migration in auth_migrations:
                text = migration.read_text(encoding="utf-8-sig")
                for statement in _sql_statements(text):
                    conn.execute(statement)
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (migration.name, digest),
                )

            role_exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,)).fetchone()
            if not role_exists:
                raise RuntimeError("Runtime role is missing. Run upgrade_multitenant.py on the application database first.")
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db_name), sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT SELECT (id, username, email, is_active, email_verified_at, session_version) ON TABLE public.users TO {}").format(sql.Identifier(app_role)))
            conn.execute(sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE public.users FROM {}").format(sql.Identifier(app_role)))
            for table in ("notes", "tags", "note_tags", "note_relations"):
                conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.{} TO {}").format(sql.Identifier(table), sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_find_user(TEXT) TO {}").format(sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_register_user(TEXT, TEXT, TEXT) TO {}").format(sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_issue_auth_token(BIGINT, TEXT, TEXT, TEXT, TIMESTAMPTZ) TO {}").format(sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_consume_email_verification(TEXT) TO {}").format(sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_consume_password_reset(TEXT, TEXT) TO {}").format(sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_find_oauth_user(TEXT, TEXT) TO {}").format(sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_list_oauth_accounts() TO {}").format(sql.Identifier(app_role)))
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_link_oauth_account(BIGINT, TEXT, TEXT, TEXT, TEXT) TO {}").format(sql.Identifier(app_role)))
            for sequence in ("notes_id_seq", "tags_id_seq", "note_relations_id_seq"):
                if conn.execute("SELECT to_regclass(%s)", (f"public.{sequence}",)).fetchone()["to_regclass"]:
                    conn.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE public.{} TO {}").format(sql.Identifier(sequence), sql.Identifier(app_role)))

    os.environ["RECALL_TEST_USER_ID"] = str(owner_id)
    return {"admin_kwargs": admin_kwargs, "app_role": app_role, "owner_id": owner_id}


@pytest.fixture
def client(test_database: dict) -> Iterator[TestClient]:
    from app.database import get_db
    from app.main import app

    admin_kwargs = test_database["admin_kwargs"]
    app_role = test_database["app_role"]
    with psycopg.connect(**admin_kwargs) as conn:
        conn.execute("TRUNCATE TABLE recall_oauth_accounts, note_relations, note_tags, notes, tags RESTART IDENTITY CASCADE")

    def override_get_db() -> Iterator[psycopg.Connection]:
        conn = psycopg.connect(**admin_kwargs)
        conn.autocommit = True
        try:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(app_role)))
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
