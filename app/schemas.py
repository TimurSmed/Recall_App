
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
