"""Seed Recall with up to TARGET_COUNT example notes using psycopg."""

import os
from itertools import cycle

import psycopg
from dotenv import load_dotenv

load_dotenv()

TARGET_COUNT = 1000

TOPICS = [
    ("Python", "Изучить генераторы, декораторы и обработку исключений."),
    ("FastAPI", "Разобраться с маршрутизацией, зависимостями и Pydantic."),
    ("PostgreSQL", "Изучить индексы, транзакции и планы выполнения запросов."),
    ("SQL", "Практиковать запросы, ограничения и транзакции без ORM."),
    ("Архитектура", "Продумать разделение ответственности между слоями приложения."),
    ("Продуктивность", "Собрать систему заметок, задач и повторения изученного."),
    ("Обучение", "Использовать активное вспоминание и интервальное повторение."),
    ("Идеи", "Записывать идеи проектов и проверять их маленькими экспериментами."),
    ("Книги", "Фиксировать ключевые мысли и практические выводы из прочитанного."),
    ("Работа", "Собирать решения проблем, инструкции и полезные рабочие заметки."),
    ("Алгоритмы", "Практиковать структуры данных, сложность и поиск."),
    ("Тестирование", "Изучить фикстуры, изоляцию тестов и проверку API."),
    ("Безопасность", "Разобраться с аутентификацией, хешированием паролей и JWT."),
    ("Git", "Практиковать ветки, коммиты и разрешение конфликтов."),
    ("Продукт", "Формулировать пользовательские проблемы и проверяемые гипотезы."),
]

DETAILS = [
    "Сначала изучить основы, затем закрепить материал небольшим практическим примером.",
    "Записать краткий конспект, пример использования и вопросы для самопроверки.",
    "Проверить подход на проекте Recall и зафиксировать полученные результаты.",
    "Сравнить несколько вариантов реализации по сложности, надёжности и поддержке.",
    "Составить чек-лист и вернуться к нему после следующего этапа разработки.",
    "Выделить типичные ошибки, способы их обнаружения и варианты исправления.",
    "Подготовить небольшой эксперимент, измерить результат и сделать вывод.",
    "Связать эту тему с уже изученными концепциями и найти практический сценарий.",
]

TAG_NAMES = [
    "изучить", "практика", "идея", "проект", "важное",
    "повторить", "разработка", "исследование", "план", "заметка",
]


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
        current_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        to_create = max(0, TARGET_COUNT - current_count)

        if to_create == 0:
            print(f"В базе уже {current_count} заметок. Добавлять ничего не нужно.")
            return

        tag_ids: dict[str, int] = {}
        for name in TAG_NAMES:
            conn.execute(
                "INSERT INTO tags (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (name,),
            )
            row = conn.execute("SELECT id FROM tags WHERE name = %s", (name,)).fetchone()
            if row is None:
                raise RuntimeError(f"Не удалось создать или найти тег {name!r}")
            tag_ids[name] = row[0]

        topics = cycle(TOPICS)
        details = cycle(DETAILS)
        tag_names = cycle(TAG_NAMES)

        for i in range(to_create):
            topic, description = next(topics)
            detail = next(details)
            selected_tag = next(tag_names)
            title = f"{topic}: заметка {current_count + i + 1:04d}"
            content = (
                f"Тема: {topic}\n\n"
                f"Основная мысль: {description}\n\n"
                f"Что сделать: {detail}\n\n"
                "Практика: применить знания в проекте Recall, "
                "проверить результат и записать выводы."
            )
            is_favorite = i % 10 == 0
            row = conn.execute(
                """
                INSERT INTO notes (title, content, is_favorite)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (title, content, is_favorite),
            ).fetchone()
            note_id = row[0]

            selected_tags = [selected_tag]
            if selected_tag != "разработка":
                selected_tags.append("разработка")

            for tag_name in selected_tags:
                conn.execute(
                    "INSERT INTO note_tags (note_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (note_id, tag_ids[tag_name]),
                )

        final_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        print(f"Готово! Было {current_count}, стало {final_count} заметок.")


if __name__ == "__main__":
    main()
