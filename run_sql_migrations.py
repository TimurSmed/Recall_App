"""Apply Recall's plain-SQL migrations without SQLAlchemy or Alembic.

Run from the project root:
    python run_sql_migrations.py

The runner can adopt a database previously managed by Alembic using the known
revision IDs. It records those migrations as applied after checking that the
expected tables/columns exist, rather than executing CREATE TABLE statements
against an existing database.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql as pg_sql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
MIGRATIONS_DIR = ROOT / "sql_migrations"

# Each legacy Alembic revision maps to the corresponding number of SQL files
# already applied in the original migration chain.
LEGACY_REVISIONS = {
    "501dbceba13e": 1,
    "1ecbdd65bac9": 2,
    "2ebac281bd91": 3,
    "d58526928071": 4,
}

EXPECTED_COLUMNS_BY_STEP = [
    {
        "notes": {"id", "title", "content", "source_url", "created_at", "updated_at"},
    },
    {
        "notes": {"id", "title", "content", "source_url", "created_at", "updated_at"},
        "tags": {"id", "name"},
        "note_tags": {"note_id", "tag_id"},
    },
    {
        "notes": {"id", "title", "content", "source_url", "created_at", "updated_at", "is_favorite"},
        "tags": {"id", "name"},
        "note_tags": {"note_id", "tag_id"},
    },
    {
        "notes": {"id", "title", "content", "source_url", "created_at", "updated_at", "is_favorite"},
        "tags": {"id", "name"},
        "note_tags": {"note_id", "tag_id"},
        "note_relations": {"id", "source_note_id", "target_note_id", "relation_type", "created_at"},
    },
]


def connect() -> psycopg.Connection:
    load_dotenv(ROOT / ".env")
    required = ["POSTGRES_USER", "POSTGRES_PASSWORD"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            "Missing database settings in .env/environment: " + ", ".join(missing)
        )
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.getenv("POSTGRES_DB", "recall"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def get_statements(sql: str) -> list[str]:
    # The current migration files contain only simple DDL statements and no
    # semicolons inside strings or procedural blocks.
    return [part.strip() for part in sql.split(";") if part.strip()]


def table_exists(conn: psycopg.Connection, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
    return row is not None and row[0] is not None


def validate_legacy_schema(conn: psycopg.Connection, step: int) -> None:
    expected = EXPECTED_COLUMNS_BY_STEP[step - 1]
    rows = conn.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ANY(%s)
        """,
        (list(expected),),
    ).fetchall()
    actual: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        actual.setdefault(table_name, set()).add(column_name)

    missing = []
    for table, columns in expected.items():
        absent = columns - actual.get(table, set())
        if absent:
            missing.append(f"{table}: {', '.join(sorted(absent))}")
    if missing:
        raise RuntimeError(
            "Alembic revision is present, but the schema does not match it. "
            "No SQL migrations were applied. Missing columns: " + "; ".join(missing)
        )


def main() -> int:
    if not MIGRATIONS_DIR.is_dir():
        raise RuntimeError(f"Migration directory not found: {MIGRATIONS_DIR}")

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No .sql migration files found in {MIGRATIONS_DIR}")

    migration_text: list[tuple[str, str, str]] = []
    for path in files:
        sql = path.read_text(encoding="utf-8")
        if not get_statements(sql):
            raise RuntimeError(f"Migration file is empty: {path.name}")
        migration_text.append((path.name, sql, checksum(sql)))

    with connect() as conn:
        # Creating this bookkeeping table does not modify any application data.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

        applied_rows = conn.execute(
            "SELECT version, checksum FROM schema_migrations"
        ).fetchall()
        applied = {version: digest for version, digest in applied_rows}

        # Import the old Alembic state once, without re-running its DDL.
        if not applied and table_exists(conn, "alembic_version"):
            revision_rows = conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
            revisions = [row[0] for row in revision_rows]
            if len(revisions) != 1 or revisions[0] not in LEGACY_REVISIONS:
                raise RuntimeError(
                    "Cannot safely adopt this Alembic database: expected one known "
                    f"revision, got {revisions!r}. No SQL migrations were applied."
                )

            legacy_revision = revisions[0]
            already_applied_count = LEGACY_REVISIONS[legacy_revision]
            if already_applied_count > len(migration_text):
                raise RuntimeError(
                    "The SQL migration directory does not contain enough files to "
                    "represent the current Alembic revision. No migrations were applied."
                )
            validate_legacy_schema(conn, already_applied_count)
            for name, _sql, digest in migration_text[:already_applied_count]:
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (name, digest),
                )
                applied[name] = digest
            print(
                f"Adopted existing Alembic revision {legacy_revision}; "
                f"recorded {already_applied_count} SQL migration(s) as already applied."
            )

        # Never blindly create tables on an untracked database. If app tables
        # exist but neither tracker identifies the schema, stop and ask for a
        # deliberate baseline instead of risking the user's existing data.
        elif not applied:
            app_tables = ("notes", "tags", "note_tags", "note_relations")
            existing_tables = [name for name in app_tables if table_exists(conn, name)]
            if existing_tables:
                raise RuntimeError(
                    "Application tables exist but neither a usable Alembic revision "
                    "nor SQL migration records were found. To protect your data, "
                    "nothing was applied. Existing tables: " + ", ".join(existing_tables)
                )

        # Finish the bootstrap/baseline transaction before applying migrations.
        # This lets every subsequent migration commit independently.
        conn.commit()

        # Apply only migrations not recorded in the tracking table.
        for name, sql, digest in migration_text:
            if name in applied:
                if applied[name] != digest:
                    raise RuntimeError(
                        f"Checksum mismatch for already-applied migration {name}. "
                        "Do not edit an applied migration; add a new numbered SQL file."
                    )
                print(f"Already applied: {name}")
                continue

            print(f"Applying: {name}")
            # Each migration and its tracking entry commit atomically.
            with conn.transaction():
                for statement in get_statements(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (name, digest),
                )
            applied[name] = digest
            print(f"Applied: {name}")

        # Re-apply auth privileges idempotently, using the configured runtime
        # role rather than assuming its name is always `recall_app`.
        app_role = os.getenv("POSTGRES_APP_USER", "recall_app").strip()
        role_exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,)
        ).fetchone()
        if role_exists and table_exists(conn, "users"):
            for function_signature in (
                "recall_issue_auth_token(bigint,text,text,text,timestamptz)",
                "recall_consume_email_verification(text)",
                "recall_consume_password_reset(text,text)",
                "recall_find_user(text)",
                "recall_find_oauth_user(text,text)",
                "recall_list_oauth_accounts()",
                "recall_link_oauth_account(bigint,text,text,text,text)",
            ):
                conn.execute(
                    pg_sql.SQL("GRANT EXECUTE ON FUNCTION public.{} TO {}").format(
                        pg_sql.SQL(function_signature), pg_sql.Identifier(app_role)
                    )
                )
            conn.execute(
                pg_sql.SQL("GRANT SELECT (email_verified_at, session_version) ON public.users TO {}").format(
                    pg_sql.Identifier(app_role)
                )
            )
        # Once our own tracker fully represents the schema, the old Alembic
        # version table is no longer required. This drops metadata only, not app data.
        recorded = {
            row[0]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        expected = {name for name, _sql, _digest in migration_text}
        if expected.issubset(recorded) and table_exists(conn, "alembic_version"):
            conn.commit()
            with conn.transaction():
                conn.execute("DROP TABLE alembic_version")
            print("Removed the obsolete alembic_version tracking table.")

    print("SQL migrations are up to date.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
