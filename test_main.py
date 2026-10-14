def test_root(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "Recall",
        "status": "running",
    }


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_notes_list_returns_list(client):
    response = client.get("/notes?limit=1&offset=0")

    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_notes_limit_validation(client):
    response = client.get("/notes?limit=0")

    assert response.status_code == 422


def test_create_note(client):
    response = client.post(
        "/notes",
        json={
            "title": "Test note",
            "content": "Testing Recall API",
            "source_url": None,
            "tags": ["testing", "python"],
        },
    )

    assert response.status_code == 201

    data = response.json()
    assert data["title"] == "Test note"
    assert data["content"] == "Testing Recall API"
    assert set(data["tags"]) == {"testing", "python"}
    assert isinstance(data["id"], int)


def test_get_missing_note_returns_404(client):
    response = client.get("/notes/999999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Note not found"


def test_get_note_by_id(client):
    create_response = client.post(
        "/notes",
        json={
            "title": "Note for reading",
            "content": "Read this note",
            "tags": ["testing"],
        },
    )

    assert create_response.status_code == 201
    note_id = create_response.json()["id"]

    response = client.get(f"/notes/{note_id}")

    assert response.status_code == 200
    assert response.json()["id"] == note_id
    assert response.json()["title"] == "Note for reading"


def test_update_note(client):
    create_response = client.post(
        "/notes",
        json={
            "title": "Old title",
            "content": "Old content",
            "tags": ["python"],
        },
    )

    assert create_response.status_code == 201
    note_id = create_response.json()["id"]

    response = client.put(
        f"/notes/{note_id}",
        json={
            "title": "New title",
            "content": "New content",
            "tags": ["backend"],
        },
    )

    assert response.status_code == 200
    assert response.json()["title"] == "New title"
    assert response.json()["content"] == "New content"
    assert response.json()["tags"] == ["backend"]


def test_delete_note(client):
    create_response = client.post(
        "/notes",
        json={
            "title": "Note to delete",
            "content": "This note will be deleted",
            "tags": [],
        },
    )

    assert create_response.status_code == 201
    note_id = create_response.json()["id"]

    delete_response = client.delete(f"/notes/{note_id}")

    assert delete_response.status_code == 204

    get_response = client.get(f"/notes/{note_id}")

    assert get_response.status_code == 404
