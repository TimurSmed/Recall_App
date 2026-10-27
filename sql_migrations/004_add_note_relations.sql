CREATE TABLE note_relations (
    id SERIAL PRIMARY KEY,
    source_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    target_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    relation_type VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_note_relations_different_notes
        CHECK (source_note_id != target_note_id),
    CONSTRAINT uq_note_relations_edge
        UNIQUE (source_note_id, target_note_id, relation_type)
);
