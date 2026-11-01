
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


def test_new_note_is_not_favorite(client):
    response = client.post(
        "/notes",
        json={
            "title": "Regular note",
            "content": "Not a favorite yet",
            "tags": [],
        },
    )

    assert response.status_code == 201
    assert response.json()["is_favorite"] is False


def test_toggle_note_favorite(client):
    create_response = client.post(
        "/notes",
        json={
            "title": "Favorite note",
            "content": "Testing favorites",
            "tags": [],
        },
    )

    assert create_response.status_code == 201
    note_id = create_response.json()["id"]

    favorite_response = client.patch(
        f"/notes/{note_id}/favorite"
    )

    assert favorite_response.status_code == 200
    assert favorite_response.json()["is_favorite"] is True

    unfavorite_response = client.patch(
        f"/notes/{note_id}/favorite"
    )

    assert unfavorite_response.status_code == 200
    assert unfavorite_response.json()["is_favorite"] is False


def test_list_favorite_notes(client):
    regular_response = client.post(
        "/notes",
        json={
            "title": "Regular note",
            "content": "Should not appear",
            "tags": [],
        },
    )

    favorite_response = client.post(
        "/notes",
        json={
            "title": "Favorite note",
            "content": "Should appear",
            "tags": [],
        },
    )

    assert regular_response.status_code == 201
    assert favorite_response.status_code == 201

    favorite_id = favorite_response.json()["id"]

    toggle_response = client.patch(
        f"/notes/{favorite_id}/favorite"
    )
    assert toggle_response.status_code == 200

    response = client.get("/notes/favorites")

    assert response.status_code == 200

    favorites = response.json()
    favorite_ids = [note["id"] for note in favorites]

    assert favorite_id in favorite_ids
    assert all(note["is_favorite"] is True for note in favorites)
    assert regular_response.json()["id"] not in favorite_ids



def test_list_notes_filter_by_favorite(client):
    regular_id = create_test_note(client, "Regular filter note")
    favorite_id = create_test_note(client, "Favorite filter note")

    toggle_response = client.patch(f"/notes/{favorite_id}/favorite")
    assert toggle_response.status_code == 200

    favorite_response = client.get("/notes?favorite=true")
    regular_response = client.get("/notes?favorite=false")

    assert favorite_response.status_code == 200
    assert regular_response.status_code == 200

    favorite_ids = [note["id"] for note in favorite_response.json()]
    regular_ids = [note["id"] for note in regular_response.json()]

    assert favorite_id in favorite_ids
    assert regular_id not in favorite_ids
    assert regular_id in regular_ids
    assert favorite_id not in regular_ids


def test_list_notes_filter_by_tag(client):
    response = client.post(
        "/notes",
        json={
            "title": "Python filter note",
            "content": "Testing tag filtering",
            "tags": ["python"],
        },
    )
    assert response.status_code == 201
    python_note_id = response.json()["id"]

    response = client.get("/notes?tag=python")

    assert response.status_code == 200
    note_ids = [note["id"] for note in response.json()]
    assert python_note_id in note_ids
    assert all(
        "python" in note["tags"]
        for note in response.json()
    )

def test_search_notes_by_tag(client):
    create_response = client.post(
        "/notes",
        json={
            "title": "A note without the keyword",
            "content": "Some unrelated content",
            "tags": ["python"],
        },
    )
    assert create_response.status_code == 201
    note_id = create_response.json()["id"]

    response = client.get("/notes/search?q=python")

    assert response.status_code == 200
    note_ids = [note["id"] for note in response.json()]
    assert note_id in note_ids


def test_search_notes_by_tag_case_insensitive(client):
    create_response = client.post(
        "/notes",
        json={
            "title": "Another unrelated note",
            "content": "Nothing relevant here",
            "tags": ["Python"],
        },
    )
    assert create_response.status_code == 201
    note_id = create_response.json()["id"]

    response = client.get("/notes/search?q=python")

    assert response.status_code == 200
    note_ids = [note["id"] for note in response.json()]
    assert note_id in note_ids



def test_list_notes_filter_by_tag_and_favorite(client):
    response = client.post(
        "/notes",
        json={
            "title": "Favorite Python note",
            "content": "Testing combined filters",
            "tags": ["python"],
        },
    )
    assert response.status_code == 201
    note_id = response.json()["id"]

    toggle_response = client.patch(f"/notes/{note_id}/favorite")
    assert toggle_response.status_code == 200

    response = client.get("/notes?favorite=true&tag=python")

    assert response.status_code == 200
    note_ids = [note["id"] for note in response.json()]
    assert note_id in note_ids
    assert all(
        note["is_favorite"] is True and "python" in note["tags"]
        for note in response.json()
    )


def test_list_notes_unknown_tag_returns_empty_list(client):
    response = client.get("/notes?tag=tag-that-does-not-exist")

    assert response.status_code == 200
    assert response.json() == []



def create_test_note(client, title):
    response = client.post(
        "/notes",
        json={
            "title": title,
            "content": f"Content of {title}",
            "tags": [],
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_create_note_relation(client):
    source_id = create_test_note(client, "Source note")
    target_id = create_test_note(client, "Target note")

    response = client.post(
        f"/notes/{source_id}/relations",
        json={
            "target_note_id": target_id,
            "relation_type": "depends_on",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["source_note_id"] == source_id
    assert data["target_note_id"] == target_id
    assert data["relation_type"] == "depends_on"


def test_create_relation_with_missing_target_returns_404(client):
    source_id = create_test_note(client, "Source note")

    response = client.post(
        f"/notes/{source_id}/relations",
        json={
            "target_note_id": 999999,
            "relation_type": "depends_on",
        },
    )

    assert response.status_code == 404


def test_create_self_relation_returns_400(client):
    note_id = create_test_note(client, "One note")

    response = client.post(
        f"/notes/{note_id}/relations",
        json={
            "target_note_id": note_id,
            "relation_type": "depends_on",
        },
    )

    assert response.status_code == 400


def test_duplicate_relation_returns_409(client):
    source_id = create_test_note(client, "Source note")
    target_id = create_test_note(client, "Target note")

    payload = {
        "target_note_id": target_id,
        "relation_type": "depends_on",
    }

    first = client.post(
        f"/notes/{source_id}/relations",
        json=payload,
    )
    second = client.post(
        f"/notes/{source_id}/relations",
        json=payload,
    )

    assert first.status_code == 201
    assert second.status_code == 409


def test_list_note_relations_returns_incoming_and_outgoing(client):
    note_a = create_test_note(client, "Note A")
    note_b = create_test_note(client, "Note B")
    note_c = create_test_note(client, "Note C")

    outgoing = client.post(
        f"/notes/{note_a}/relations",
        json={
            "target_note_id": note_b,
            "relation_type": "depends_on",
        },
    )
    incoming = client.post(
        f"/notes/{note_c}/relations",
        json={
            "target_note_id": note_a,
            "relation_type": "explains",
        },
    )

    assert outgoing.status_code == 201
    assert incoming.status_code == 201

    response = client.get(f"/notes/{note_a}/relations")

    assert response.status_code == 200
    relations = response.json()
    assert len(relations) == 2
    assert {item["source_note_id"] for item in relations} == {note_a, note_c}


def test_delete_note_relation(client):
    source_id = create_test_note(client, "Source note")
    target_id = create_test_note(client, "Target note")

    create_response = client.post(
        f"/notes/{source_id}/relations",
        json={
            "target_note_id": target_id,
            "relation_type": "depends_on",
        },
    )
    assert create_response.status_code == 201
    relation_id = create_response.json()["id"]

    delete_response = client.delete(
        f"/notes/{source_id}/relations/{relation_id}"
    )
    assert delete_response.status_code == 204

    list_response = client.get(f"/notes/{source_id}/relations")
    assert list_response.status_code == 200
    assert list_response.json() == []


def test_delete_missing_relation_returns_404(client):
    source_id = create_test_note(client, "Source note")

    response = client.delete(
        f"/notes/{source_id}/relations/999999"
    )

    assert response.status_code == 404


def test_graph_returns_notes_at_correct_depth(client):
    note_a = create_test_note(client, "Python")
    note_b = create_test_note(client, "Variables")
    note_c = create_test_note(client, "Data types")

    first = client.post(
        f"/notes/{note_a}/relations",
        json={"target_note_id": note_b, "relation_type": "learns"},
    )
    second = client.post(
        f"/notes/{note_b}/relations",
        json={"target_note_id": note_c, "relation_type": "explains"},
    )

    assert first.status_code == 201
    assert second.status_code == 201

    response = client.get(f"/notes/{note_a}/graph?depth=2")

    assert response.status_code == 200
    data = response.json()

    assert data["start_note_id"] == note_a
    assert data["depth"] == 2

    notes_by_id = {note["id"]: note for note in data["notes"]}
    assert set(notes_by_id) == {note_b, note_c}
    assert notes_by_id[note_b]["depth"] == 1
    assert notes_by_id[note_c]["depth"] == 2
    assert len(data["relations"]) == 2


def test_graph_with_invalid_depth_returns_422(client):
    note_id = create_test_note(client, "Python")

    response = client.get(f"/notes/{note_id}/graph?depth=6")

    assert response.status_code == 422


def test_graph_with_missing_note_returns_404(client):
    response = client.get("/notes/999999/graph")

    assert response.status_code == 404


def test_graph_handles_cycles_without_duplicates(client):
    note_a = create_test_note(client, "Note A")
    note_b = create_test_note(client, "Note B")
    note_c = create_test_note(client, "Note C")

    assert client.post(
        f"/notes/{note_a}/relations",
        json={"target_note_id": note_b, "relation_type": "related"},
    ).status_code == 201

    assert client.post(
        f"/notes/{note_b}/relations",
        json={"target_note_id": note_c, "relation_type": "related"},
    ).status_code == 201

    assert client.post(
        f"/notes/{note_c}/relations",
        json={"target_note_id": note_a, "relation_type": "related"},
    ).status_code == 201

    response = client.get(f"/notes/{note_a}/graph?depth=5")

    assert response.status_code == 200
    data = response.json()

    found_ids = [note["id"] for note in data["notes"]]
    assert len(found_ids) == len(set(found_ids))
    assert set(found_ids) == {note_b, note_c}


def test_graph_loads_all_direct_neighbors_in_star_graph(client):
    note_ids = [
        create_test_note(client, f"Note {i}")
        for i in range(6)
    ]

    # One source note connected to five neighbors.
    for target_id in note_ids[1:]:
        response = client.post(
            f"/notes/{note_ids[0]}/relations",
            json={
                "target_note_id": target_id,
                "relation_type": "related",
            },
        )
        assert response.status_code == 201

    response = client.get(f"/notes/{note_ids[0]}/graph?depth=1")

    assert response.status_code == 200
    data = response.json()
    notes_by_id = {note["id"]: note for note in data["notes"]}
    assert set(notes_by_id) == set(note_ids[1:])
    assert all(note["depth"] == 1 for note in notes_by_id.values())
    assert len(data["relations"]) == 5


def test_users_cannot_access_each_others_notes(client, test_database):
    import psycopg

    owner_note = client.post(
        "/notes",
        json={"title": "Private owner note", "content": "Only the owner can read this", "tags": ["private"]},
    )
    assert owner_note.status_code == 201
    owner_note_id = owner_note.json()["id"]

    with psycopg.connect(**test_database["admin_kwargs"]) as conn:
        second_user = conn.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s) RETURNING id",
            ("second_user", "second@example.invalid", "unused-test-hash"),
        ).fetchone()
        second_user_id = second_user["id"]

    import os
    original_user_id = os.environ["RECALL_TEST_USER_ID"]
    try:
        os.environ["RECALL_TEST_USER_ID"] = str(second_user_id)
        hidden = client.get(f"/notes/{owner_note_id}")
        assert hidden.status_code == 404
        assert client.get("/notes").json() == []

        second_note = client.post(
            "/notes",
            json={"title": "Second user's note", "content": "Private", "tags": ["private"]},
        )
        assert second_note.status_code == 201
        second_note_id = second_note.json()["id"]
    finally:
        os.environ["RECALL_TEST_USER_ID"] = original_user_id

    visible = client.get("/notes")
    assert visible.status_code == 200
    visible_ids = {item["id"] for item in visible.json()}
    assert owner_note_id in visible_ids
    assert second_note_id not in visible_ids


def test_registration_creates_a_separate_account(client):
    response = client.post(
        "/auth/register",
        json={
            "username": "new_member",
            "email": "new-member@example.com",
            "password": "Long-Unique-Passphrase-42",
        },
    )
    assert response.status_code == 201
    assert response.json()["username"] == "new_member"
    assert "recall_session" in response.cookies


def test_password_recovery_token_is_single_use_and_verifies_email(client, test_database):
    import hashlib
    import secrets
    import psycopg

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    with psycopg.connect(**test_database["admin_kwargs"]) as conn:
        conn.execute(
            """SELECT * FROM public.recall_issue_auth_token(
                   %s, NULL, 'password_reset', %s, now() + interval '30 minutes'
               )""",
            (test_database["owner_id"], token_hash),
        ).fetchone()

    response = client.post(
        "/auth/password/reset",
        json={"token": token, "password": "New-Unique-Password-2026"},
    )
    assert response.status_code == 200

    with psycopg.connect(**test_database["admin_kwargs"]) as conn:
        user = conn.execute(
            "SELECT password_hash, session_version, email_verified_at FROM users WHERE id = %s",
            (test_database["owner_id"],),
        ).fetchone()
    from app.main import _verify_password

    assert _verify_password("New-Unique-Password-2026", user["password_hash"])
    assert user["session_version"] == 1
    assert user["email_verified_at"] is not None

    reused = client.post(
        "/auth/password/reset",
        json={"token": token, "password": "Another-Unique-Password-2026"},
    )
    assert reused.status_code == 400


def test_email_verification_token_is_single_use(client, test_database):
    import hashlib
    import secrets
    import psycopg

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    with psycopg.connect(**test_database["admin_kwargs"]) as conn:
        conn.execute(
            """SELECT * FROM public.recall_issue_auth_token(
                   %s, NULL, 'verify_email', %s, now() + interval '24 hours'
               )""",
            (test_database["owner_id"], token_hash),
        ).fetchone()

    response = client.post("/auth/email/verify", json={"token": token})
    assert response.status_code == 200
    reused = client.post("/auth/email/verify", json={"token": token})
    assert reused.status_code == 400


def test_users_cannot_create_or_read_relations_to_other_users_notes(client, test_database):
    import os
    import psycopg

    owner_note = client.post(
        "/notes",
        json={"title": "Owner private note", "content": "Only owner", "tags": []},
    )
    assert owner_note.status_code == 201
    owner_note_id = owner_note.json()["id"]

    with psycopg.connect(**test_database["admin_kwargs"]) as admin:
        second_user = admin.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s) RETURNING id",
            ("relation_second_user", "relation-second@example.invalid", "unused-test-hash"),
        ).fetchone()
        second_user_id = int(second_user["id"])

    original_user_id = os.environ["RECALL_TEST_USER_ID"]
    try:
        os.environ["RECALL_TEST_USER_ID"] = str(second_user_id)
        second_note = client.post(
            "/notes",
            json={"title": "Second user's note", "content": "Private", "tags": []},
        )
        assert second_note.status_code == 201
        second_note_id = second_note.json()["id"]

        attempt = client.post(
            f"/notes/{second_note_id}/relations",
            json={"target_note_id": owner_note_id, "relation_type": "related"},
        )
        assert attempt.status_code == 404
        assert client.get(f"/notes/{owner_note_id}/relations").status_code == 404
    finally:
        os.environ["RECALL_TEST_USER_ID"] = original_user_id

    owner_relations = client.get(f"/notes/{owner_note_id}/relations")
    assert owner_relations.status_code == 200
    assert owner_relations.json() == []
