"""Database-side multi-tenant schema helpers for Recall."""
from __future__ import annotations

import hashlib
from psycopg import Connection


def apply_multitenant_schema(
    conn: Connection,
    *,
    owner_username: str,
    owner_password_hash: str,
    owner_email: str | None = None,
) -> int:
    """Upgrade the existing single-user data in-place and return its owner ID.

    The caller must run this inside one transaction on the administrative DB role.
    Re-running it is safe: populated owner IDs are never reassigned.
    """
    username = owner_username.strip().lower()
    if not username or not owner_password_hash:
        raise ValueError("A bootstrap username and password hash are required")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username VARCHAR(32) NOT NULL,
            email VARCHAR(254),
            password_hash TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_lower ON users (lower(username))"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (lower(email)) WHERE email IS NOT NULL"
    )

    owner = conn.execute(
        "SELECT id FROM users WHERE lower(username) = %s", (username,)
    ).fetchone()
    if owner is None:
        owner = conn.execute(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (username, owner_email.lower() if owner_email else None, owner_password_hash),
        ).fetchone()
    owner_id = int(owner["id"])

    # Add nullable ownership columns first; then backfill existing single-user data.
    for table in ("notes", "tags", "note_tags", "note_relations"):
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS user_id BIGINT')

    conn.execute("UPDATE notes SET user_id = %s WHERE user_id IS NULL", (owner_id,))
    conn.execute("UPDATE tags SET user_id = %s WHERE user_id IS NULL", (owner_id,))
    conn.execute(
        """
        UPDATE note_tags nt SET user_id = n.user_id
        FROM notes n WHERE nt.note_id = n.id AND nt.user_id IS NULL
        """
    )
    conn.execute(
        """
        UPDATE note_relations r SET user_id = n.user_id
        FROM notes n WHERE r.source_note_id = n.id AND r.user_id IS NULL
        """
    )

    # Refuse to continue if pre-existing edges ever cross owner boundaries.
    mismatched_tags = conn.execute(
        """
        SELECT count(*) AS n
        FROM note_tags nt
        JOIN notes n ON n.id = nt.note_id
        JOIN tags t ON t.id = nt.tag_id
        WHERE n.user_id <> t.user_id OR nt.user_id <> n.user_id
        """
    ).fetchone()["n"]
    mismatched_relations = conn.execute(
        """
        SELECT count(*) AS n
        FROM note_relations r
        JOIN notes s ON s.id = r.source_note_id
        JOIN notes t ON t.id = r.target_note_id
        WHERE s.user_id <> t.user_id OR r.user_id <> s.user_id
        """
    ).fetchone()["n"]
    if mismatched_tags or mismatched_relations:
        raise RuntimeError(
            "Existing cross-owner tag/relation records were detected. "
            "Migration stopped without changing ownership."
        )

    # Attach all owned records to a valid user. The existing rows are assigned
    # to the bootstrap owner; future rows must always supply user_id.
    for table in ("notes", "tags", "note_tags", "note_relations"):
        null_count = conn.execute(
            f'SELECT count(*) AS n FROM "{table}" WHERE user_id IS NULL'
        ).fetchone()["n"]
        if null_count:
            raise RuntimeError(f"Cannot safely migrate {table}: {null_count} rows lack an owner")
        conn.execute(f'ALTER TABLE "{table}" ALTER COLUMN user_id SET NOT NULL')
        conn.execute(
            f'ALTER TABLE "{table}" ADD CONSTRAINT "{table}_user_id_fkey" '
            f'FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE'
        ) if not _constraint_exists(conn, f"{table}_user_id_fkey", table) else None

    # Parent unique keys allow composite FKs to enforce same-owner relations.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_notes_id_user_id ON notes (id, user_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_tags_id_user_id ON tags (id, user_id)")

    # Remove the old global tag-name uniqueness and replace it with per-user uniqueness.
    conn.execute("ALTER TABLE tags DROP CONSTRAINT IF EXISTS tags_name_key")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_tags_user_name ON tags (user_id, name)")

    # Rebuild note_tags FKs as composite owner-aware FKs.
    conn.execute("ALTER TABLE note_tags DROP CONSTRAINT IF EXISTS note_tags_note_id_fkey")
    conn.execute("ALTER TABLE note_tags DROP CONSTRAINT IF EXISTS note_tags_tag_id_fkey")
    conn.execute("ALTER TABLE note_tags DROP CONSTRAINT IF EXISTS fk_note_tags_note_owner")
    conn.execute("ALTER TABLE note_tags DROP CONSTRAINT IF EXISTS fk_note_tags_tag_owner")
    conn.execute(
        "ALTER TABLE note_tags ADD CONSTRAINT fk_note_tags_note_owner "
        "FOREIGN KEY (note_id, user_id) REFERENCES notes(id, user_id) ON DELETE CASCADE"
    )
    conn.execute(
        "ALTER TABLE note_tags ADD CONSTRAINT fk_note_tags_tag_owner "
        "FOREIGN KEY (tag_id, user_id) REFERENCES tags(id, user_id) ON DELETE CASCADE"
    )

    # Rebuild relation FKs to guarantee both endpoints belong to the relation owner.
    conn.execute("ALTER TABLE note_relations DROP CONSTRAINT IF EXISTS note_relations_source_note_id_fkey")
    conn.execute("ALTER TABLE note_relations DROP CONSTRAINT IF EXISTS note_relations_target_note_id_fkey")
    conn.execute("ALTER TABLE note_relations DROP CONSTRAINT IF EXISTS fk_relations_source_owner")
    conn.execute("ALTER TABLE note_relations DROP CONSTRAINT IF EXISTS fk_relations_target_owner")
    conn.execute(
        "ALTER TABLE note_relations ADD CONSTRAINT fk_relations_source_owner "
        "FOREIGN KEY (source_note_id, user_id) REFERENCES notes(id, user_id) ON DELETE CASCADE"
    )
    conn.execute(
        "ALTER TABLE note_relations ADD CONSTRAINT fk_relations_target_owner "
        "FOREIGN KEY (target_note_id, user_id) REFERENCES notes(id, user_id) ON DELETE CASCADE"
    )

    for table in ("notes", "tags", "note_tags", "note_relations"):
        conn.execute(f'CREATE INDEX IF NOT EXISTS "ix_{table}_user_id" ON "{table}" (user_id)')
        conn.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        conn.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        conn.execute(f'DROP POLICY IF EXISTS recall_owner_isolation ON "{table}"')
        conn.execute(
            f"""CREATE POLICY recall_owner_isolation ON \"{table}\"
                USING (user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT)
                WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT)"""
        )

    # Users are also protected by RLS. Unlike application tables, this table is
    # not FORCEd so the table-owner SECURITY DEFINER functions below can perform
    # login lookup and registration without exposing the table to the app role.
    conn.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    conn.execute("DROP POLICY IF EXISTS recall_user_self ON users")
    conn.execute(
        """CREATE POLICY recall_user_self ON users
           USING (id = NULLIF(current_setting('app.user_id', true), '')::BIGINT)
           WITH CHECK (id = NULLIF(current_setting('app.user_id', true), '')::BIGINT)"""
    )
    conn.execute(
        """CREATE OR REPLACE FUNCTION public.recall_find_user(p_identifier TEXT)
           RETURNS TABLE(id BIGINT, username VARCHAR(32), email VARCHAR(254), password_hash TEXT, is_active BOOLEAN)
           LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public
           AS $$
             SELECT u.id, u.username, u.email, u.password_hash, u.is_active
             FROM public.users AS u
             WHERE lower(u.username) = lower(p_identifier)
                OR lower(u.email) = lower(p_identifier)
             LIMIT 1
           $$"""
    )
    conn.execute(
        """CREATE OR REPLACE FUNCTION public.recall_register_user(
               p_username TEXT, p_email TEXT, p_password_hash TEXT
           ) RETURNS TABLE(id BIGINT, username VARCHAR(32), email VARCHAR(254))
           LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public
           AS $$
             INSERT INTO public.users (username, email, password_hash)
             VALUES (lower(p_username), lower(p_email), p_password_hash)
             ON CONFLICT DO NOTHING
             RETURNING users.id, users.username, users.email
           $$"""
    )
    conn.execute("REVOKE ALL ON FUNCTION public.recall_find_user(TEXT) FROM PUBLIC")
    conn.execute("REVOKE ALL ON FUNCTION public.recall_register_user(TEXT, TEXT, TEXT) FROM PUBLIC")

    # Record this upgrade in the existing plain-SQL migration ledger.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    marker = hashlib.sha256(b"recall-multitenant-schema-v1").hexdigest()
    conn.execute(
        """INSERT INTO schema_migrations (version, checksum)
           VALUES ('005_multi_tenant', %s)
           ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum""",
        (marker,),
    )
    return owner_id


def _constraint_exists(conn: Connection, constraint_name: str, table_name: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM pg_constraint c
           JOIN pg_class t ON t.oid = c.conrelid
           JOIN pg_namespace n ON n.oid = t.relnamespace
           WHERE n.nspname = 'public' AND t.relname = %s AND c.conname = %s""",
        (table_name, constraint_name),
    ).fetchone() is not None
