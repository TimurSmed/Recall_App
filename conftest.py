import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from sqlalchemy import URL, delete, create_engine
from sqlalchemy.orm import Session

load_dotenv()

from app.database import Base, get_db
from app.main import app
from app.models import Note, Tag, note_tags


@pytest.fixture(scope="session")
def test_engine():
    test_db_name = os.getenv("POSTGRES_TEST_DB")

    if not test_db_name:
        raise RuntimeError(
            "POSTGRES_TEST_DB is missing from .env"
        )

    database_url = URL.create(
        "postgresql+psycopg",
        username=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=test_db_name,
    )

    engine = create_engine(database_url)

    # Пересоздаём таблицы только в отдельной тестовой базе.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    yield engine

    engine.dispose()


@pytest.fixture
def client(test_engine):
    # Удаляем данные перед каждым тестом.
    with test_engine.begin() as connection:
        connection.execute(delete(note_tags))
        connection.execute(delete(Note))
        connection.execute(delete(Tag))

    def override_get_db():
        with Session(test_engine) as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.pop(get_db, None)