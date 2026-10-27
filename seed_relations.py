"""Create a sample graph of related notes using psycopg."""

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def connection_kwargs() -> dict:
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    if not user or password is None:
        raise RuntimeError("POSTGRES_USER и POSTGRES_PASSWORD должны быть заданы в .env")

    return {
        "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "dbname": os.getenv("POSTGRES_DB", "recall"),
        "user": user,
        "password": password,
    }


def main() -> None:
    with psycopg.connect(**connection_kwargs()) as conn:
        note_ids = [row[0] for row in conn.execute("SELECT id FROM notes ORDER BY id").fetchall()]

        if len(note_ids) < 3:
            print("Нужно минимум 3 заметки.")
            return

        existing_related = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT source_note_id, target_note_id FROM note_relations WHERE relation_type = %s",
                ("related",),
            ).fetchall()
        }

        added = 0
        # Цепочка с дополнительными рёбрами для альтернативных путей.
        for i, source_id in enumerate(note_ids):
            for offset in (1, 3, 7):
                j = i + offset
                if j >= len(note_ids):
                    continue

                target_id = note_ids[j]
                pair = (source_id, target_id)
                if pair in existing_related:
                    continue

                cursor = conn.execute(
                    """
                    INSERT INTO note_relations (source_note_id, target_note_id, relation_type)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (source_note_id, target_note_id, relation_type) DO NOTHING
                    """,
                    (source_id, target_id, "related"),
                )
                if cursor.rowcount:
                    existing_related.add(pair)
                    added += 1

        print(f"Добавлено связей: {added}")
        print(f"Всего заметок: {len(note_ids)}")


if __name__ == "__main__":
    main()
