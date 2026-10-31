"""One-time, data-preserving upgrade from single-user Recall to multi-user Recall.

Run from the project root with the existing administrative PostgreSQL credentials
in .env (POSTGRES_USER/POSTGRES_PASSWORD). The script creates a least-privilege
runtime role and writes POSTGRES_APP_USER/POSTGRES_APP_PASSWORD to .env.
"""
from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
sys.path.insert(0, str(ROOT))
from app.multitenant import apply_multitenant_schema  # noqa: E402


def set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    prefix = key + "="
    replaced = False
    output = []
    for line in lines:
        if line.strip().startswith(prefix):
            if not replaced:
                output.append(f"{key}={value}")
                replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    load_dotenv(ENV_PATH, override=True)
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "RECALL_USERNAME", "RECALL_PASSWORD_HASH")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing .env settings: " + ", ".join(missing))
    if not os.getenv("RECALL_SECRET_KEY"):
        raise RuntimeError("RECALL_SECRET_KEY is missing. Keep the existing session secret in .env.")
    if len(os.environ["RECALL_USERNAME"].strip()) > 32:
        raise RuntimeError("The existing RECALL_USERNAME exceeds 32 characters; shorten it before migration.")

    admin_user = os.environ["POSTGRES_USER"]
    app_user = os.getenv("POSTGRES_APP_USER", "recall_app").strip()
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", app_user):
        raise RuntimeError("POSTGRES_APP_USER must be a simple PostgreSQL role name (letters, digits, underscore).")
    if app_user.lower() == admin_user.lower():
        raise RuntimeError("Runtime DB role must differ from POSTGRES_USER. Use recall_app.")

    app_password = os.getenv("POSTGRES_APP_PASSWORD") or secrets.token_urlsafe(36)
    db_name = os.getenv("POSTGRES_DB", "recall")
    conn = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=db_name,
        user=admin_user,
        password=os.environ["POSTGRES_PASSWORD"],
        row_factory=dict_row,
    )
    try:
        with conn.transaction():
            owner_id = apply_multitenant_schema(
                conn,
                owner_username=os.environ["RECALL_USERNAME"],
                owner_password_hash=os.environ["RECALL_PASSWORD_HASH"],
                owner_email=os.getenv("RECALL_EMAIL") or None,
            )

            role_exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (app_user,)
            ).fetchone()
            if role_exists:
                conn.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT NOBYPASSRLS").format(
                        sql.Identifier(app_user), sql.Literal(app_password)
                    )
                )
            else:
                conn.execute(
                    sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT NOBYPASSRLS").format(
                        sql.Identifier(app_user), sql.Literal(app_password)
                    )
                )

            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db_name), sql.Identifier(app_user)))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(app_user)))
            conn.execute(
                sql.SQL("GRANT SELECT (id, username, email, is_active) ON TABLE public.users TO {}").format(sql.Identifier(app_user))
            )
            conn.execute(
                sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE public.users FROM {}").format(sql.Identifier(app_user))
            )
            for table in ("notes", "tags", "note_tags", "note_relations"):
                conn.execute(
                    sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.{} TO {}").format(
                        sql.Identifier(table), sql.Identifier(app_user)
                    )
                )
            conn.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_find_user(TEXT) TO {}").format(sql.Identifier(app_user))
            )
            conn.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION public.recall_register_user(TEXT, TEXT, TEXT) TO {}").format(sql.Identifier(app_user))
            )
            for sequence in ("notes_id_seq", "tags_id_seq", "note_relations_id_seq"):
                if conn.execute("SELECT to_regclass(%s)", (f"public.{sequence}",)).fetchone()["to_regclass"]:
                    conn.execute(
                        sql.SQL("GRANT USAGE, SELECT ON SEQUENCE public.{} TO {}").format(
                            sql.Identifier(sequence), sql.Identifier(app_user)
                        )
                    )

            # Remove any inherited role memberships if this role pre-existed.
            memberships = conn.execute(
                """SELECT parent.rolname
                   FROM pg_auth_members m
                   JOIN pg_roles parent ON parent.oid = m.roleid
                   JOIN pg_roles member ON member.oid = m.member
                   WHERE member.rolname = %s""",
                (app_user,),
            ).fetchall()
            for membership in memberships:
                conn.execute(
                    sql.SQL("REVOKE {} FROM {}").format(
                        sql.Identifier(membership["rolname"]), sql.Identifier(app_user)
                    )
                )

            # Ensure the runtime role is never a table owner/superuser and thus
            # cannot bypass FORCE ROW LEVEL SECURITY.
            role_flags = conn.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (app_user,)
            ).fetchone()
            if role_flags["rolsuper"] or role_flags["rolbypassrls"]:
                raise RuntimeError("Runtime database role must be NOSUPERUSER and NOBYPASSRLS.")

        set_env_value(ENV_PATH, "POSTGRES_APP_USER", app_user)
        set_env_value(ENV_PATH, "POSTGRES_APP_PASSWORD", app_password)
        print(f"Multi-user schema is ready. Existing notes were assigned to user id {owner_id} ({os.environ['RECALL_USERNAME']}).")
        print("Created/updated the least-privilege PostgreSQL runtime role.")
        print("Saved POSTGRES_APP_USER and POSTGRES_APP_PASSWORD to .env (the password was not printed).")
        print("Next: replace app/main.py and app/database.py with the supplied files, then run tests.")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Multi-user upgrade failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
