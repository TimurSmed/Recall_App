from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg import sql


def test_oauth_start_requires_provider_credentials(client, monkeypatch):
    monkeypatch.delenv("RECALL_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("RECALL_GITHUB_CLIENT_SECRET", raising=False)
    response = client.get("/auth/oauth/github/start", follow_redirects=False)
    assert response.status_code == 503
    assert "Client ID" in response.json()["detail"]


def test_github_oauth_start_uses_state_pkce_and_configured_callback(client, monkeypatch):
    monkeypatch.setenv("RECALL_GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("RECALL_GITHUB_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("RECALL_PUBLIC_BASE_URL", "https://recall.example")
    monkeypatch.setenv("RECALL_SECRET_KEY", base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="))

    response = client.get("/auth/oauth/github/start", follow_redirects=False)

    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    query = parse_qs(location.query)
    assert location.netloc == "github.com"
    assert query["redirect_uri"] == ["https://recall.example/auth/oauth/github/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["code_challenge"][0]) == 43
    assert len(query["state"][0]) >= 32
    assert "recall_oauth_github" in response.headers.get("set-cookie", "")


def test_yandex_oauth_start_uses_email_and_profile_scopes(client, monkeypatch):
    monkeypatch.setenv("RECALL_YANDEX_CLIENT_ID", "test-yandex-id")
    monkeypatch.setenv("RECALL_YANDEX_CLIENT_SECRET", "test-yandex-secret")
    monkeypatch.setenv("RECALL_PUBLIC_BASE_URL", "https://recall.example")
    monkeypatch.setenv("RECALL_SECRET_KEY", base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="))

    response = client.get("/auth/oauth/yandex/start", follow_redirects=False)

    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    query = parse_qs(location.query)
    assert location.netloc == "oauth.yandex.ru"
    assert query["redirect_uri"] == ["https://recall.example/auth/oauth/yandex/callback"]
    assert set(query["scope"][0].split()) == {"login:info", "login:email"}
    assert query["force_confirm"] == ["yes"]
    assert query["code_challenge_method"] == ["S256"]


def test_oauth_callback_rejects_missing_state_cookie(client):
    response = client.get("/auth/oauth/github/callback?code=fake&state=fake", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?oauth=invalid"


def test_oauth_account_link_is_unique_and_visible_only_to_current_user(test_database):
    admin_kwargs = test_database["admin_kwargs"]
    app_role = test_database["app_role"]
    owner_id = int(test_database["owner_id"])

    with psycopg.connect(**admin_kwargs) as conn:
        with conn.transaction():
            other = conn.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s) RETURNING id",
                ("oauth_other_user", "oauth-other@example.invalid", "unused-test-hash"),
            ).fetchone()
            other_id = int(other["id"])

        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(app_role)))
        conn.execute("SELECT set_config('app.user_id', %s, false)", (str(owner_id),))

        first = conn.execute(
            "SELECT public.recall_link_oauth_account(%s, %s, %s, %s, %s) AS result",
            (owner_id, "github", "gh-123", "recall-dev", "dev@example.invalid"),
        ).fetchone()
        assert first["result"] == "linked"

        repeated = conn.execute(
            "SELECT public.recall_link_oauth_account(%s, %s, %s, %s, %s) AS result",
            (owner_id, "github", "gh-123", "recall-dev", "dev@example.invalid"),
        ).fetchone()
        assert repeated["result"] == "already_linked"

        conn.execute("SELECT set_config('app.user_id', %s, false)", (str(other_id),))
        conflict = conn.execute(
            "SELECT public.recall_link_oauth_account(%s, %s, %s, %s, %s) AS result",
            (other_id, "github", "gh-123", "other", "other@example.invalid"),
        ).fetchone()
        assert conflict["result"] == "identity_in_use"

        provider_already_linked = conn.execute(
            "SELECT public.recall_link_oauth_account(%s, %s, %s, %s, %s) AS result",
            (other_id, "github", "gh-456", "other", "other@example.invalid"),
        ).fetchone()
        assert provider_already_linked["result"] == "linked"
        second_provider_identity = conn.execute(
            "SELECT public.recall_link_oauth_account(%s, %s, %s, %s, %s) AS result",
            (other_id, "github", "gh-789", "other", "other@example.invalid"),
        ).fetchone()
        assert second_provider_identity["result"] == "provider_already_linked"

        conn.execute("SELECT set_config('app.user_id', %s, false)", (str(owner_id),))
        lookup = conn.execute(
            "SELECT * FROM public.recall_find_oauth_user(%s, %s)",
            ("github", "gh-123"),
        ).fetchone()
        assert int(lookup["user_id"]) == owner_id

        accounts = conn.execute("SELECT * FROM public.recall_list_oauth_accounts()").fetchall()
        assert len(accounts) == 1
        assert accounts[0]["provider"] == "github"
        assert accounts[0]["provider_username"] == "recall-dev"
