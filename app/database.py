import os
from collections.abc import Generator

import psycopg
from dotenv import load_dotenv
from psycopg import Connection
from psycopg.rows import dict_row

load_dotenv()


def get_db() -> Generator[Connection, None, None]:
    """Open a request-scoped connection using the non-superuser app role."""
    app_user = os.getenv("POSTGRES_APP_USER")
    app_password = os.getenv("POSTGRES_APP_PASSWORD")
    if not app_user or not app_password:
        raise RuntimeError(
            "Multi-user database role is not configured. Run upgrade_multitenant.py first."
        )
    conn = psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.getenv("POSTGRES_DB", "recall"),
        user=app_user,
        password=app_password,
        row_factory=dict_row,
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()
