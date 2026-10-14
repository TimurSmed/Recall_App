from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.models import Note, Tag
from app.schemas import NoteCreate, NoteRead, NoteUpdate

from pathlib import Path

app = FastAPI(
    title="Recall",
    description="Personal knowledge management API",
    version="0.1.0",
)



def get_or_create_tags(db: Session, tag_names: list[str]) -> list[Tag]:
    tags = []
    seen_names = set()

    for raw_name in tag_names:
        name = raw_name.strip()

        if not name:
            continue

        normalized_name = name.lower()

        if normalized_name in seen_names:
            continue

        seen_names.add(normalized_name)

        tag = db.query(Tag).filter(Tag.name == normalized_name).first()

        if tag is None:
            tag = Tag(name=normalized_name)
            db.add(tag)
            db.flush()

        tags.append(tag)

    return tags


def note_to_dict(note: Note) -> dict:
    return {
        "id": note.id,
        "title": note.title,
        "content": note.content,
        "is_favorite": note.is_favorite,
        "source_url": note.source_url,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
        "tags": [tag.name for tag in note.tags],
    }




@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "Recall",
        "status": "running",
    }


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/health/db")
def database_health_check() -> dict[str, str]:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    return {
        "status": "healthy",
        "database": "connected",
    }



@app.post("/notes", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
def create_note(
    note_data: NoteCreate,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    data = note_data.model_dump(exclude={"tags"})

    note = Note(**data)
    note.tags = get_or_create_tags(db, note_data.tags)

    db.add(note)
    db.commit()
    db.refresh(note)

    return note_to_dict(note)



@app.get("/notes", response_model=list[NoteRead])
def list_notes(
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict]:
    notes = (
        db.query(Note)
        .order_by(Note.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return [note_to_dict(note) for note in notes]


@app.get("/notes/favorites", response_model=list[NoteRead])
def list_favorite_notes(
    db: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    notes = (
        db.query(Note)
        .filter(Note.is_favorite.is_(True))
        .order_by(Note.created_at.desc())
        .all()
    )

    return [note_to_dict(note) for note in notes]




@app.get("/tags")
def list_tags(
    db: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    tags = db.query(Tag).order_by(Tag.name).all()

    return [
        {
            "id": tag.id,
            "name": tag.name,
            "notes_count": len(tag.notes),
        }
        for tag in tags
    ]


@app.get("/tags/{tag_name}/notes", response_model=list[NoteRead])
def get_notes_by_tag(
    tag_name: str,
    db: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    normalized_name = tag_name.strip().lower()

    tag = db.query(Tag).filter(Tag.name == normalized_name).first()

    if tag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tag not found",
        )

    notes = (
        db.query(Note)
        .join(Note.tags)
        .filter(Tag.name == normalized_name)
        .order_by(Note.created_at.desc())
        .all()
    )

    return [note_to_dict(note) for note in notes]




@app.get("/notes/search", response_model=list[NoteRead])
def search_notes(
    q: Annotated[str, Query(min_length=1, description="Search notes by title or content")],
    db: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    search_term = f"%{q}%"

    notes = (
        db.query(Note)
        .filter(
            Note.title.ilike(search_term)
            | Note.content.ilike(search_term)
        )
        .order_by(Note.created_at.desc())
        .all()
    )

    return [note_to_dict(note) for note in notes]

@app.get("/notes/{note_id}", response_model=NoteRead)
def get_note(
    note_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    note = db.get(Note, note_id)

    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        )

    return note_to_dict(note)


@app.patch("/notes/{note_id}/favorite", response_model=NoteRead)
def toggle_note_favorite(
    note_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    note = db.get(Note, note_id)

    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        )

    note.is_favorite = not note.is_favorite

    db.commit()
    db.refresh(note)

    return note_to_dict(note)


@app.put("/notes/{note_id}", response_model=NoteRead)
def update_note(
    note_id: int,
    note_data: NoteUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    note = db.get(Note, note_id)

    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        )

    # Обновляем поля заметки, исключая теги
    data = note_data.model_dump(exclude={"tags"})

    for field, value in data.items():
        setattr(note, field, value)

    # Заменяем теги заметки на переданные
    note.tags = get_or_create_tags(db, note_data.tags)

    db.commit()
    db.refresh(note)

    return note_to_dict(note)



@app.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(
    note_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    note = db.get(Note, note_id)

    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        )

    db.delete(note)
    db.commit()

app.mount(
    "/ui",
    StaticFiles(
        directory=Path(__file__).resolve().parent / "static",
        html=True,
    ),
    name="ui",
)









