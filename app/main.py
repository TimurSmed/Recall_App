from collections import deque
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.staticfiles import StaticFiles
from psycopg import Connection
from psycopg.rows import dict_row

from app.database import get_db
from app.schemas import (
    GraphNoteRead,
    GraphRead,
    NoteCreate,
    NoteRead,
    NoteUpdate,
    RelationCreate,
    RelationRead,
)

app = FastAPI(
    title="Recall",
    description="Personal knowledge management API",
    version="0.1.0",
)


def get_or_create_tags(conn: Connection, tag_names: list[str]) -> list[str]:
    """Normalize tag names and ensure each exists, returning unique names."""
    tags: list[str] = []
    seen_names: set[str] = set()

    for raw_name in tag_names:
        name = raw_name.strip().lower()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        conn.execute(
            "INSERT INTO tags (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
            (name,),
        )
        tags.append(name)
    return tags


def set_note_tags(conn: Connection, note_id: int, tag_names: list[str]) -> list[str]:
    tags = get_or_create_tags(conn, tag_names)
    conn.execute("DELETE FROM note_tags WHERE note_id = %s", (note_id,))
    if tags:
        conn.execute(
            """
            INSERT INTO note_tags (note_id, tag_id)
            SELECT %s, id FROM tags WHERE name = ANY(%s)
            ON CONFLICT DO NOTHING
            """,
            (note_id, tags),
        )
    return tags


def fetch_note(conn: Connection, note_id: int) -> dict | None:
    return conn.execute(
        """
        SELECT id, title, content, is_favorite, source_url, created_at, updated_at
        FROM notes WHERE id = %s
        """,
        (note_id,),
    ).fetchone()


def note_to_dict(conn: Connection, note: dict) -> dict:
    tags = conn.execute(
        """
        SELECT t.name
        FROM tags AS t
        JOIN note_tags AS nt ON nt.tag_id = t.id
        WHERE nt.note_id = %s
        ORDER BY t.name
        """,
        (note["id"],),
    ).fetchall()
    return {**note, "tags": [row["name"] for row in tags]}


def fetch_notes_with_tags(conn: Connection, sql: str, params: tuple = ()) -> list[dict]:
    rows = conn.execute(sql, params).fetchall()
    return [note_to_dict(conn, row) for row in rows]


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "Recall", "status": "running"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/health/db")
def database_health_check(
    conn: Annotated[Connection, Depends(get_db)],
) -> dict[str, str]:
    conn.execute("SELECT 1").fetchone()
    return {"status": "healthy", "database": "connected"}


@app.post("/notes", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
def create_note(
    note_data: NoteCreate,
    conn: Annotated[Connection, Depends(get_db)],
) -> dict:
    data = note_data.model_dump(exclude={"tags"})
    with conn.transaction():
        row = conn.execute(
            """
            INSERT INTO notes (title, content, source_url)
            VALUES (%s, %s, %s)
            RETURNING id, title, content, is_favorite, source_url, created_at, updated_at
            """,
            (data["title"], data["content"], data.get("source_url")),
        ).fetchone()
        set_note_tags(conn, row["id"], note_data.tags)
        result = note_to_dict(conn, row)
    return result


@app.get("/notes", response_model=list[NoteRead])
def list_notes(
    conn: Annotated[Connection, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    favorite: bool | None = None,
    tag: str | None = None,
    q: str | None = None,
    sort_by: str = Query(default="created_at", pattern="^(created_at|updated_at|title)$"),
) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    if favorite is not None:
        conditions.append("n.is_favorite = %s")
        params.append(favorite)
    if tag is not None:
        conditions.append("EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id WHERE nt.note_id = n.id AND t.name = %s)")
        params.append(tag.strip().lower())
    if q is not None and q.strip():
        conditions.append("(n.title ILIKE %s OR n.content ILIKE %s OR EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id WHERE nt.note_id = n.id AND t.name ILIKE %s))")
        term = f"%{q.strip()}%"
        params.extend([term, term, term])
    where_sql = " WHERE " + " AND ".join(conditions) if conditions else ""
    order_sql = {"title": "n.title ASC", "updated_at": "n.updated_at DESC", "created_at": "n.created_at DESC"}[sort_by]
    params.extend([limit, offset])
    return fetch_notes_with_tags(
        conn,
        f"SELECT n.id, n.title, n.content, n.is_favorite, n.source_url, n.created_at, n.updated_at FROM notes n{where_sql} ORDER BY {order_sql} LIMIT %s OFFSET %s",
        tuple(params),
    )


@app.get("/notes/favorites", response_model=list[NoteRead])
def list_favorite_notes(conn: Annotated[Connection, Depends(get_db)]) -> list[dict]:
    return fetch_notes_with_tags(
        conn,
        "SELECT id, title, content, is_favorite, source_url, created_at, updated_at FROM notes WHERE is_favorite = TRUE ORDER BY created_at DESC",
    )


@app.get("/tags")
def list_tags(conn: Annotated[Connection, Depends(get_db)]) -> list[dict]:
    rows = conn.execute(
        """
        SELECT t.id, t.name, COUNT(nt.note_id)::int AS notes_count
        FROM tags t LEFT JOIN note_tags nt ON nt.tag_id = t.id
        GROUP BY t.id, t.name ORDER BY t.name
        """
    ).fetchall()
    return [dict(row) for row in rows]


@app.get("/tags/{tag_name}/notes", response_model=list[NoteRead])
def get_notes_by_tag(tag_name: str, conn: Annotated[Connection, Depends(get_db)]) -> list[dict]:
    normalized_name = tag_name.strip().lower()
    exists = conn.execute("SELECT 1 FROM tags WHERE name = %s", (normalized_name,)).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return fetch_notes_with_tags(
        conn,
        """SELECT n.id, n.title, n.content, n.is_favorite, n.source_url, n.created_at, n.updated_at
           FROM notes n JOIN note_tags nt ON nt.note_id = n.id
           JOIN tags t ON t.id = nt.tag_id WHERE t.name = %s ORDER BY n.created_at DESC""",
        (normalized_name,),
    )


@app.get("/notes/search", response_model=list[NoteRead])
def search_notes(
    q: Annotated[str, Query(min_length=1, description="Search notes by title or content")],
    conn: Annotated[Connection, Depends(get_db)],
) -> list[dict]:
    term = f"%{q}%"
    return fetch_notes_with_tags(
        conn,
        """SELECT n.id, n.title, n.content, n.is_favorite, n.source_url, n.created_at, n.updated_at
           FROM notes n WHERE n.title ILIKE %s OR n.content ILIKE %s
           OR EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id
                      WHERE nt.note_id = n.id AND t.name ILIKE %s)
           ORDER BY n.created_at DESC""",
        (term, term, term),
    )


@app.get("/notes/{note_id}", response_model=NoteRead)
def get_note(note_id: int, conn: Annotated[Connection, Depends(get_db)]) -> dict:
    note = fetch_note(conn, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note_to_dict(conn, note)


@app.post("/notes/{note_id}/relations", response_model=RelationRead, status_code=status.HTTP_201_CREATED)
def create_note_relation(
    note_id: int,
    relation_data: RelationCreate,
    conn: Annotated[Connection, Depends(get_db)],
) -> dict:
    with conn.transaction():
        if fetch_note(conn, note_id) is None:
            raise HTTPException(status_code=404, detail="Source note not found")
        if fetch_note(conn, relation_data.target_note_id) is None:
            raise HTTPException(status_code=404, detail="Target note not found")
        if note_id == relation_data.target_note_id:
            raise HTTPException(status_code=400, detail="A note cannot be related to itself")
        relation_type = relation_data.relation_type.strip().lower()
        if not relation_type:
            raise HTTPException(status_code=422, detail="Relation type cannot be empty")
        try:
            row = conn.execute(
                """INSERT INTO note_relations (source_note_id, target_note_id, relation_type)
                   VALUES (%s, %s, %s)
                   RETURNING id, source_note_id, target_note_id, relation_type, created_at""",
                (note_id, relation_data.target_note_id, relation_type),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(status_code=409, detail="This relation already exists") from None
    return dict(row)


@app.get("/notes/{note_id}/relations", response_model=list[RelationRead])
def list_note_relations(note_id: int, conn: Annotated[Connection, Depends(get_db)]) -> list[dict]:
    if fetch_note(conn, note_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    rows = conn.execute(
        """SELECT id, source_note_id, target_note_id, relation_type, created_at
           FROM note_relations WHERE source_note_id = %s OR target_note_id = %s ORDER BY id""",
        (note_id, note_id),
    ).fetchall()
    return [dict(row) for row in rows]


@app.delete("/notes/{note_id}/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note_relation(
    note_id: int,
    relation_id: int,
    conn: Annotated[Connection, Depends(get_db)],
) -> None:
    with conn.transaction():
        row = conn.execute(
            """DELETE FROM note_relations WHERE id = %s
               AND (source_note_id = %s OR target_note_id = %s) RETURNING id""",
            (relation_id, note_id, note_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Relation not found")


@app.get("/notes/{note_id}/graph", response_model=GraphRead)
def get_note_graph(
    note_id: int,
    conn: Annotated[Connection, Depends(get_db)],
    depth: int = Query(default=2, ge=1, le=5),
) -> GraphRead:
    start_note = fetch_note(conn, note_id)
    if start_note is None:
        raise HTTPException(status_code=404, detail="Note not found")

    visited = {note_id}
    frontier = {note_id}
    found_notes: list[GraphNoteRead] = []
    found_relations: dict[int, dict] = {}

    for current_depth in range(depth):
        if not frontier:
            break
        frontier_ids = list(frontier)
        rows = conn.execute(
            """SELECT r.id, r.source_note_id, r.target_note_id, r.relation_type, r.created_at,
                      CASE WHEN r.source_note_id = ANY(%s) THEN r.target_note_id ELSE r.source_note_id END AS neighbor_id,
                      n.title AS neighbor_title
               FROM note_relations r
               JOIN notes n ON n.id = CASE WHEN r.source_note_id = ANY(%s)
                                           THEN r.target_note_id ELSE r.source_note_id END
               WHERE r.source_note_id = ANY(%s) OR r.target_note_id = ANY(%s)
               ORDER BY r.id""",
            (frontier_ids, frontier_ids, frontier_ids, frontier_ids),
        ).fetchall()
        next_frontier: set[int] = set()
        for row in rows:
            relation = {key: row[key] for key in ("id", "source_note_id", "target_note_id", "relation_type", "created_at")}
            found_relations[row["id"]] = relation
            neighbor_id = row["neighbor_id"]
            if neighbor_id not in visited:
                visited.add(neighbor_id)
                next_frontier.add(neighbor_id)
                found_notes.append(GraphNoteRead(id=neighbor_id, title=row["neighbor_title"], depth=current_depth + 1))
        frontier = next_frontier

    return GraphRead(
        start_note_id=note_id,
        depth=depth,
        notes=found_notes,
        relations=[RelationRead(**row) for row in found_relations.values()],
    )


@app.patch("/notes/{note_id}/favorite", response_model=NoteRead)
def toggle_note_favorite(note_id: int, conn: Annotated[Connection, Depends(get_db)]) -> dict:
    with conn.transaction():
        row = conn.execute(
            """UPDATE notes SET is_favorite = NOT is_favorite
               WHERE id = %s
               RETURNING id, title, content, is_favorite, source_url, created_at, updated_at""",
            (note_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Note not found")
        result = note_to_dict(conn, row)
    return result


@app.put("/notes/{note_id}", response_model=NoteRead)
def update_note(
    note_id: int,
    note_data: NoteUpdate,
    conn: Annotated[Connection, Depends(get_db)],
) -> dict:
    data = note_data.model_dump(exclude={"tags"})
    with conn.transaction():
        current = fetch_note(conn, note_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Note not found")
        row = conn.execute(
            """UPDATE notes SET title = %s, content = %s, source_url = %s, updated_at = now()
               WHERE id = %s
               RETURNING id, title, content, is_favorite, source_url, created_at, updated_at""",
            (data["title"], data["content"], data.get("source_url"), note_id),
        ).fetchone()
        set_note_tags(conn, note_id, note_data.tags)
        result = note_to_dict(conn, row)
    return result


@app.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(note_id: int, conn: Annotated[Connection, Depends(get_db)]) -> None:
    with conn.transaction():
        row = conn.execute("DELETE FROM notes WHERE id = %s RETURNING id", (note_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Note not found")


app.mount(
    "/ui",
    StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True),
    name="ui",
)
