from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.models import Note, NoteRelation, Tag
from app.schemas import (
    GraphNoteRead,
    GraphRead,
    NoteCreate,
    NoteRead,
    NoteUpdate,
    RelationCreate,
    RelationRead,
)

from pathlib import Path
from collections import deque

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
    favorite: bool | None = None,
    tag: str | None = None,
    q: str | None = None,
    sort_by: str = Query(
        default="created_at",
        pattern="^(created_at|updated_at|title)$",
    ),
) -> list[dict]:
    query = db.query(Note)

    if favorite is not None:
        query = query.filter(Note.is_favorite.is_(favorite))

    if tag is not None:
        query = query.filter(
            Note.tags.any(Tag.name == tag.strip().lower())
    )
        
    if q is not None and q.strip():
        search_term = f"%{q.strip()}%"
        query = query.filter(
            Note.title.ilike(search_term)
            | Note.content.ilike(search_term)
            | Note.tags.any(Tag.name.ilike(search_term))
        )


    
    if sort_by == "title":
        query = query.order_by(Note.title.asc())
    elif sort_by == "updated_at":
        query = query.order_by(Note.updated_at.desc())
    else:
        query = query.order_by(Note.created_at.desc())

    notes = query.offset(offset).limit(limit).all()


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
            | Note.tags.any(Tag.name.ilike(search_term))
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


@app.post(
    "/notes/{note_id}/relations",
    response_model=RelationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_note_relation(
    note_id: int,
    relation_data: RelationCreate,
    db: Annotated[Session, Depends(get_db)],
) -> NoteRelation:
    source_note = db.get(Note, note_id)

    if source_note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source note not found",
        )

    target_note = db.get(Note, relation_data.target_note_id)

    if target_note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target note not found",
        )

    if note_id == relation_data.target_note_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A note cannot be related to itself",
        )

    relation_type = relation_data.relation_type.strip().lower()

    if not relation_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Relation type cannot be empty",
        )

    existing_relation = (
        db.query(NoteRelation)
        .filter(
            NoteRelation.source_note_id == note_id,
            NoteRelation.target_note_id == relation_data.target_note_id,
            NoteRelation.relation_type == relation_type,
        )
        .first()
    )

    if existing_relation is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This relation already exists",
        )

    relation = NoteRelation(
        source_note_id=note_id,
        target_note_id=relation_data.target_note_id,
        relation_type=relation_type,
    )

    db.add(relation)
    db.commit()
    db.refresh(relation)

    return relation


@app.get(
    "/notes/{note_id}/relations",
    response_model=list[RelationRead],
)
def list_note_relations(
    note_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> list[NoteRelation]:
    note = db.get(Note, note_id)

    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        )

    relations = (
        db.query(NoteRelation)
        .filter(
            (NoteRelation.source_note_id == note_id)
            | (NoteRelation.target_note_id == note_id)
        )
        .order_by(NoteRelation.id)
        .all()
    )

    return relations

@app.delete(
    "/notes/{note_id}/relations/{relation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_note_relation(
    note_id: int,
    relation_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    relation = (
        db.query(NoteRelation)
        .filter(
            NoteRelation.id == relation_id,
            (
                (NoteRelation.source_note_id == note_id)
                | (NoteRelation.target_note_id == note_id)
            ),
        )
        .first()
    )

    if relation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Relation not found",
        )

    db.delete(relation)
    db.commit()




@app.get(
    "/notes/{note_id}/graph",
    response_model=GraphRead,
)
def get_note_graph(
    note_id: int,
    depth: int = 2,
    db: Annotated[Session, Depends(get_db)] = None,
) -> GraphRead:
    if depth < 1 or depth > 5:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Depth must be between 1 and 5",
        )

    start_note = db.get(Note, note_id)
    if start_note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Note not found",
        )

    visited = {note_id}
    frontier = {note_id}
    found_notes = []
    found_relations = {}

    for current_depth in range(depth):
        if not frontier:
            break

        relations = (
            db.query(NoteRelation)
            .filter(
                (NoteRelation.source_note_id.in_(frontier))
                | (NoteRelation.target_note_id.in_(frontier))
            )
            .order_by(NoteRelation.id)
            .all()
        )

        next_frontier = set()
        new_note_ids = set()

        for relation in relations:
            found_relations[relation.id] = relation

            if relation.source_note_id in frontier:
                neighbor_id = relation.target_note_id
            else:
                neighbor_id = relation.source_note_id

            if neighbor_id not in visited:
                visited.add(neighbor_id)
                new_note_ids.add(neighbor_id)
                next_frontier.add(neighbor_id)

        if new_note_ids:
            neighbors = (
                db.query(Note)
                .filter(Note.id.in_(new_note_ids))
                .all()
            )
            neighbors_by_id = {
                note.id: note for note in neighbors
            }

            for neighbor_id in new_note_ids:
                neighbor = neighbors_by_id.get(neighbor_id)
                if neighbor is None:
                    continue

                found_notes.append(
                    GraphNoteRead(
                        id=neighbor.id,
                        title=neighbor.title,
                        depth=current_depth + 1,
                    )
                )

        frontier = next_frontier

    return GraphRead(
        start_note_id=note_id,
        depth=depth,
        notes=found_notes,
        relations=[
            RelationRead.model_validate(
                relation,
                from_attributes=True,
            )
            for relation in found_relations.values()
        ],
    )




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









