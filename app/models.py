from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base



note_tags = Table(
    "note_tags",
    Base.metadata,
    Column("note_id", ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    is_favorite: Mapped[bool] = mapped_column(
        default=False,
        server_default="false",
        nullable=False,
    )
    source_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    tags: Mapped[list["Tag"]] = relationship(
    secondary=note_tags,
    back_populates="notes",
    )
    outgoing_relations: Mapped[list["NoteRelation"]] = relationship(
        foreign_keys="NoteRelation.source_note_id",
        back_populates="source_note",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    incoming_relations: Mapped[list["NoteRelation"]] = relationship(
        foreign_keys="NoteRelation.target_note_id",
        back_populates="target_note",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


    
class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)

    notes: Mapped[list["Note"]] = relationship(
        secondary=note_tags,
        back_populates="tags",
    )



class NoteRelation(Base):
    __tablename__ = "note_relations"

    __table_args__ = (
        CheckConstraint(
            "source_note_id != target_note_id",
            name="ck_note_relations_different_notes",
        ),
        UniqueConstraint(
            "source_note_id",
            "target_note_id",
            "relation_type",
            name="uq_note_relations_edge",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    source_note_id: Mapped[int] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
    )

    target_note_id: Mapped[int] = mapped_column(
        ForeignKey("notes.id", ondelete="CASCADE"),
        nullable=False,
    )

    relation_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    source_note: Mapped["Note"] = relationship(
        foreign_keys=[source_note_id],
        back_populates="outgoing_relations",
    )

    target_note: Mapped["Note"] = relationship(
        foreign_keys=[target_note_id],
        back_populates="incoming_relations",
    )

