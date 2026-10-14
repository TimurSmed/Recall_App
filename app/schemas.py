
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NoteCreate(BaseModel):
    title: str
    content: str
    source_url: str | None = None
    tags: list[str] = Field(default_factory=list)


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    source_url: str | None
    is_favorite: bool = False
    created_at: datetime
    updated_at: datetime
    tags: list[str] = Field(default_factory=list)


class NoteUpdate(BaseModel):
    title: str
    content: str
    source_url: str | None = None
    tags: list[str] = Field(default_factory=list)



class RelationCreate(BaseModel):
    target_note_id: int = Field(gt=0)
    relation_type: str = Field(min_length=1, max_length=50)


class RelationRead(BaseModel):
    id: int
    source_note_id: int
    target_note_id: int
    relation_type: str
    created_at: datetime


class GraphNoteRead(BaseModel):
    id: int
    title: str
    depth: int


class GraphRead(BaseModel):
    start_note_id: int
    depth: int
    notes: list[GraphNoteRead]
    relations: list[RelationRead]

