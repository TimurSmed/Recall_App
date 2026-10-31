from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from psycopg import Connection
from psycopg.rows import dict_row

from app.database import get_db
from app.schemas import (
    GraphNoteRead,
    GraphRead,
    NoteCreate,
    NoteRead,
    NoteUpdate,
    RelationCreate,
    RelationRead,
)

app = FastAPI(
    title="Recall",
    description="Personal knowledge management for every account",
    version="0.2.0",
)

SESSION_COOKIE = "recall_session"
DEFAULT_SESSION_TTL = 8 * 60 * 60
PASSWORD_ITERATIONS = 600_000
LOGIN_FAILURES: dict[str, tuple[int, float]] = {}
REGISTER_FAILURES: dict[str, tuple[int, float]] = {}


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _session_secret() -> tuple[bytes, int] | None:
    secret_text = os.getenv("RECALL_SECRET_KEY", "").strip()
    try:
        secret = _b64url_decode(secret_text)
        ttl = int(os.getenv("RECALL_SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL)))
        if len(secret) < 32 or ttl < 300 or ttl > 60 * 60 * 24 * 30:
            return None
        return secret, ttl
    except (ValueError, TypeError):
        return None


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS, _b64url_encode(salt), _b64url_encode(digest)
    )


def _verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded_hash.split("$", 3)
        iterations = int(iterations_text)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _b64url_decode(salt_text), iterations
        )
        return hmac.compare_digest(actual, _b64url_decode(digest_text))
    except (ValueError, TypeError):
        return False


def _make_session(user_id: int, secret: bytes, ttl: int) -> str:
    payload = {
        "sub": int(user_id),
        "exp": int(time.time()) + ttl,
        "nonce": secrets.token_urlsafe(16),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + _b64url_encode(signature)


def _session_user_id(token: str | None) -> int | None:
    config = _session_secret()
    if not token or config is None:
        return None
    secret, _ttl = config
    try:
        body, supplied_signature = token.split(".", 1)
        expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(supplied_signature)):
            return None
        payload = json.loads(_b64url_decode(body))
        user_id = int(payload.get("sub", 0))
        if user_id < 1 or int(payload.get("exp", 0)) <= int(time.time()):
            return None
        return user_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _cookie_secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if forwarded == "https" or request.url.scheme == "https":
        return True
    # Cloudflare also identifies the external scheme in CF-Visitor.
    try:
        visitor = json.loads(request.headers.get("cf-visitor", "{}"))
        return visitor.get("scheme", "").lower() == "https"
    except (TypeError, json.JSONDecodeError):
        return False


def _set_session_cookie(response: Response, request: Request, user_id: int) -> None:
    config = _session_secret()
    if config is None:
        raise HTTPException(status_code=503, detail="RECALL_SECRET_KEY is missing or invalid")
    secret, ttl = config
    response.set_cookie(
        key=SESSION_COOKIE,
        value=_make_session(user_id, secret, ttl),
        max_age=ttl,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
    )


def _rate_limit(bucket: dict[str, tuple[int, float]], key: str, limit: int = 8) -> None:
    now = time.monotonic()
    attempts, started = bucket.get(key, (0, now))
    if now - started > 900:
        attempts, started = 0, now
    if attempts >= limit:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Попробуй через 15 минут.")
    bucket[key] = (attempts + 1, started)


def _client_key(request: Request) -> str:
    # Cloudflare overwrites CF-Connecting-IP; otherwise use the socket peer.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


LOGIN_HTML = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Вход — Recall</title>
<style>
:root{color-scheme:dark;--bg:#080a12;--panel:rgba(18,22,38,.9);--line:rgba(169,181,219,.17);--text:#f2f4ff;--muted:#929bb5;--violet:#a18aff;--cyan:#65d9ed}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(ellipse at 14% 12%,#7650ce35,transparent 38%),radial-gradient(ellipse at 88% 85%,#2697b42a,transparent 35%),var(--bg);font:15px/1.55 Inter,Segoe UI,Arial,sans-serif;color:var(--text)}.card{width:min(100%,430px);padding:38px;border:1px solid var(--line);border-radius:25px;background:var(--panel);box-shadow:0 28px 100px #0009;backdrop-filter:blur(22px)}.mark{width:48px;height:48px;display:grid;place-items:center;border-radius:16px;background:linear-gradient(135deg,#a18aff,#65d9ed);font-size:23px;font-weight:900;color:#090a12}h1{margin:24px 0 4px;font-size:30px;letter-spacing:-1.2px}p{margin:0 0 25px;color:var(--muted)}label{display:block;margin:16px 0 7px;font-size:13px;color:#c9cfe3}input{width:100%;padding:13px 14px;border:1px solid var(--line);border-radius:12px;background:#0b0e19;color:var(--text);font:inherit;outline:none}input:focus{border-color:var(--violet);box-shadow:0 0 0 3px #a18aff20}button{width:100%;margin-top:23px;padding:13px 16px;border:0;border-radius:12px;background:linear-gradient(110deg,#a18aff,#65d9ed);color:#10101b;font-weight:800;font-size:15px;cursor:pointer}button:disabled{opacity:.65;cursor:wait}#error{min-height:20px;margin-top:14px;color:#fda4af;font-size:13px}.foot{margin-top:22px;color:#8d92a9;font-size:13px;text-align:center}.foot a{color:#c4b5fd;text-decoration:none}
</style></head><body><main class="card"><div class="mark">R</div><h1>С возвращением</h1><p>Войди, чтобы открыть свою базу знаний.</p><form id="login-form"><label for="identifier">Логин или email</label><input id="identifier" name="identifier" autocomplete="username" required maxlength="254"><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="128"><button id="submit" type="submit">Войти в Recall</button><div id="error" role="alert"></div></form><div class="foot">Впервые здесь? <a href="/register">Создать аккаунт</a></div></main>
<script>const form=document.querySelector('#login-form'),error=document.querySelector('#error'),button=document.querySelector('#submit');form.addEventListener('submit',async e=>{e.preventDefault();error.textContent='';button.disabled=true;button.textContent='Проверяем...';try{const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({identifier:form.identifier.value.trim(),password:form.password.value})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'Не удалось войти');location.replace('/ui/')}catch(err){error.textContent=err.message||'Ошибка соединения'}finally{button.disabled=false;button.textContent='Войти в Recall'}});</script></body></html>'''

REGISTER_HTML = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Регистрация — Recall</title>
<style>
:root{color-scheme:dark;--bg:#080a12;--panel:rgba(18,22,38,.9);--line:rgba(169,181,219,.17);--text:#f2f4ff;--muted:#929bb5;--violet:#a18aff;--cyan:#65d9ed}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(ellipse at 14% 12%,#7650ce35,transparent 38%),radial-gradient(ellipse at 88% 85%,#2697b42a,transparent 35%),var(--bg);font:15px/1.55 Inter,Segoe UI,Arial,sans-serif;color:var(--text)}.card{width:min(100%,450px);padding:38px;border:1px solid var(--line);border-radius:25px;background:var(--panel);box-shadow:0 28px 100px #0009;backdrop-filter:blur(22px)}.mark{width:48px;height:48px;display:grid;place-items:center;border-radius:16px;background:linear-gradient(135deg,#a18aff,#65d9ed);font-size:23px;font-weight:900;color:#090a12}h1{margin:24px 0 4px;font-size:30px;letter-spacing:-1.2px}p{margin:0 0 22px;color:var(--muted)}label{display:block;margin:14px 0 7px;font-size:13px;color:#c9cfe3}input{width:100%;padding:13px 14px;border:1px solid var(--line);border-radius:12px;background:#0b0e19;color:var(--text);font:inherit;outline:none}input:focus{border-color:var(--violet);box-shadow:0 0 0 3px #a18aff20}button{width:100%;margin-top:22px;padding:13px 16px;border:0;border-radius:12px;background:linear-gradient(110deg,#a18aff,#65d9ed);color:#10101b;font-weight:800;font-size:15px;cursor:pointer}button:disabled{opacity:.65;cursor:wait}#error{min-height:20px;margin-top:14px;color:#fda4af;font-size:13px}.hint{margin-top:8px;color:#858da8;font-size:11px}.foot{margin-top:20px;color:#8d92a9;font-size:13px;text-align:center}.foot a{color:#c4b5fd;text-decoration:none}
</style></head><body><main class="card"><div class="mark">R</div><h1>Создай свой Recall</h1><p>Личное пространство для заметок, идей и связей.</p><form id="register-form"><label for="username">Имя пользователя</label><input id="username" name="username" autocomplete="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9_.-]+" required><div class="hint">3–32 символа: латинские буквы, цифры, точка, дефис или подчёркивание.</div><label for="email">Email</label><input id="email" name="email" type="email" autocomplete="email" maxlength="254" required><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required><div class="hint">Не менее 12 символов.</div><label for="confirm">Повтори пароль</label><input id="confirm" name="confirm" type="password" autocomplete="new-password" minlength="12" maxlength="128" required><button id="submit" type="submit">Создать аккаунт</button><div id="error" role="alert"></div></form><div class="foot">Уже есть аккаунт? <a href="/login">Войти</a></div></main>
<script>const form=document.querySelector('#register-form'),error=document.querySelector('#error'),button=document.querySelector('#submit');form.addEventListener('submit',async e=>{e.preventDefault();error.textContent='';if(form.password.value!==form.confirm.value){error.textContent='Пароли не совпадают.';return}button.disabled=true;button.textContent='Создаём аккаунт...';try{const r=await fetch('/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:form.username.value.trim(),email:form.email.value.trim(),password:form.password.value})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'Не удалось создать аккаунт');location.replace('/ui/')}catch(err){error.textContent=err.message||'Ошибка соединения'}finally{button.disabled=false;button.textContent='Создать аккаунт'}});</script></body></html>'''


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    if os.getenv("RECALL_TESTING") == "1":
        return await call_next(request)

    path = request.url.path
    # Apply CSRF checks to public auth forms as well as authenticated mutations.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            try:
                origin_host = urlsplit(origin).netloc.lower()
                request_host = request.headers.get("host", "").lower()
                if not origin_host or origin_host != request_host:
                    return JSONResponse({"detail": "Origin check failed"}, status_code=403)
            except ValueError:
                return JSONResponse({"detail": "Invalid Origin header"}, status_code=403)

    public_paths = {"/login", "/register", "/auth/login", "/auth/register", "/logout", "/health"}
    if path in public_paths:
        return await call_next(request)

    secret_config = _session_secret()
    if secret_config is None:
        return JSONResponse(
            {"detail": "Authentication is not configured. Set RECALL_SECRET_KEY in .env."},
            status_code=503,
        )

    user_id = _session_user_id(request.cookies.get(SESSION_COOKIE))
    if user_id is None:
        if request.method == "GET" and (path == "/" or path.startswith("/ui") or path in {"/docs", "/redoc", "/openapi.json"}):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Authentication required"}, status_code=401)

    request.state.auth_user_id = user_id
    return await call_next(request)


# Dependency that verifies the user in PostgreSQL and sets the RLS context on
# this request-scoped connection. The runtime DB role is NOBYPASSRLS.
def get_current_user(
    request: Request,
    conn: Annotated[Connection, Depends(get_db)],
) -> dict:
    if os.getenv("RECALL_TESTING") == "1":
        raw_user_id = os.getenv("RECALL_TEST_USER_ID", "1")
        try:
            user_id = int(raw_user_id)
        except ValueError:
            raise HTTPException(status_code=500, detail="Invalid test user id") from None
    else:
        user_id = getattr(request.state, "auth_user_id", None)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")

    # Session-level setting is safe because get_db opens and closes one
    # connection per request. Set it before reading users so users-RLS exposes
    # only this account's own profile row.
    conn.execute("SELECT set_config('app.user_id', %s, false)", (str(user_id),))
    user = conn.execute(
        "SELECT id, username, email FROM users WHERE id = %s AND is_active = TRUE",
        (user_id,),
    ).fetchone()
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    return dict(user)


CurrentUser = Annotated[dict, Depends(get_current_user)]


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML)


@app.get("/register", response_class=HTMLResponse)
def register_page() -> HTMLResponse:
    return HTMLResponse(REGISTER_HTML)


@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    conn: Annotated[Connection, Depends(get_db)],
) -> dict[str, str]:
    key = _client_key(request)
    _rate_limit(REGISTER_FAILURES, key, limit=10)
    if _session_secret() is None:
        raise HTTPException(status_code=503, detail="RECALL_SECRET_KEY is missing or invalid")

    username = payload.username.strip().lower()
    email = str(payload.email).strip().lower()
    password_hash = _hash_password(payload.password)
    with conn.transaction():
        row = conn.execute(
            "SELECT * FROM public.recall_register_user(%s, %s, %s)",
            (username, email, password_hash),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="Это имя пользователя или email уже зарегистрированы.")

    _set_session_cookie(response, request, int(row["id"]))
    return {"status": "registered", "username": row["username"]}


@app.post("/auth/login")
def login(
    credentials: LoginRequest,
    request: Request,
    response: Response,
    conn: Annotated[Connection, Depends(get_db)],
) -> dict[str, str]:
    secret_config = _session_secret()
    if secret_config is None:
        raise HTTPException(status_code=503, detail="RECALL_SECRET_KEY is missing or invalid")

    key = _client_key(request)
    attempts, started = LOGIN_FAILURES.get(key, (0, time.monotonic()))
    if time.monotonic() - started > 900:
        attempts, started = 0, time.monotonic()
    if attempts >= 8:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Попробуй через 15 минут.")

    identifier = credentials.identifier.strip().lower()
    user = conn.execute(
        "SELECT * FROM public.recall_find_user(%s) WHERE is_active = TRUE",
        (identifier,),
    ).fetchone()
    # Hash verification also runs for unknown users to reduce timing-based enumeration.
    dummy_hash = _hash_password("not-a-real-password")
    valid_hash = _verify_password(credentials.password, user["password_hash"] if user else dummy_hash)
    if user is None or not valid_hash:
        LOGIN_FAILURES[key] = (attempts + 1, started)
        raise HTTPException(status_code=401, detail="Неверный логин/email или пароль.")

    LOGIN_FAILURES.pop(key, None)
    _set_session_cookie(response, request, int(user["id"]))
    return {"status": "ok", "username": user["username"]}


@app.post("/auth/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=_cookie_secure(request), samesite="lax")
    return {"status": "logged_out"}


@app.get("/logout")
def logout_from_browser(request: Request) -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=_cookie_secure(request), samesite="lax")
    return response


@app.get("/auth/me")
def auth_me(current_user: CurrentUser) -> dict[str, str | None]:
    return {"username": current_user["username"], "email": current_user["email"]}


def get_or_create_tags(conn: Connection, user_id: int, tag_names: list[str]) -> list[str]:
    tags: list[str] = []
    seen_names: set[str] = set()
    for raw_name in tag_names:
        name = raw_name.strip().lower()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        conn.execute(
            "INSERT INTO tags (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id, name) DO NOTHING",
            (user_id, name),
        )
        tags.append(name)
    return tags


def set_note_tags(conn: Connection, user_id: int, note_id: int, tag_names: list[str]) -> list[str]:
    tags = get_or_create_tags(conn, user_id, tag_names)
    conn.execute("DELETE FROM note_tags WHERE note_id = %s AND user_id = %s", (note_id, user_id))
    if tags:
        conn.execute(
            """INSERT INTO note_tags (note_id, tag_id, user_id)
               SELECT %s, id, %s FROM tags WHERE user_id = %s AND name = ANY(%s)
               ON CONFLICT DO NOTHING""",
            (note_id, user_id, user_id, tags),
        )
    return tags


def fetch_note(conn: Connection, note_id: int, user_id: int) -> dict | None:
    return conn.execute(
        """SELECT id, title, content, is_favorite, source_url, created_at, updated_at
           FROM notes WHERE id = %s AND user_id = %s""",
        (note_id, user_id),
    ).fetchone()


def note_to_dict(conn: Connection, note: dict, user_id: int) -> dict:
    tags = conn.execute(
        """SELECT t.name FROM tags t JOIN note_tags nt ON nt.tag_id = t.id
           AND nt.user_id = t.user_id
           WHERE nt.note_id = %s AND nt.user_id = %s ORDER BY t.name""",
        (note["id"], user_id),
    ).fetchall()
    return {**note, "tags": [row["name"] for row in tags]}


def fetch_notes_with_tags(conn: Connection, sql_text: str, params: tuple, user_id: int) -> list[dict]:
    rows = conn.execute(sql_text, params).fetchall()
    return [note_to_dict(conn, row, user_id) for row in rows]


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "Recall", "status": "running"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/health/db")
def database_health_check(
    conn: Annotated[Connection, Depends(get_db)],
    current_user: CurrentUser,
) -> dict[str, str]:
    conn.execute("SELECT 1").fetchone()
    return {"status": "healthy", "database": "connected"}


@app.post("/notes", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
def create_note(
    note_data: NoteCreate,
    conn: Annotated[Connection, Depends(get_db)],
    current_user: CurrentUser,
) -> dict:
    user_id = int(current_user["id"])
    data = note_data.model_dump(exclude={"tags"})
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO notes (user_id, title, content, source_url)
               VALUES (%s, %s, %s, %s)
               RETURNING id, title, content, is_favorite, source_url, created_at, updated_at""",
            (user_id, data["title"], data["content"], data.get("source_url")),
        ).fetchone()
        set_note_tags(conn, user_id, row["id"], note_data.tags)
        result = note_to_dict(conn, row, user_id)
    return result


@app.get("/notes", response_model=list[NoteRead])
def list_notes(
    conn: Annotated[Connection, Depends(get_db)],
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    favorite: bool | None = None,
    tag: str | None = None,
    q: str | None = None,
    sort_by: str = Query(default="created_at", pattern="^(created_at|updated_at|title)$"),
) -> list[dict]:
    user_id = int(current_user["id"])
    conditions = ["n.user_id = %s"]
    params: list = [user_id]
    if favorite is not None:
        conditions.append("n.is_favorite = %s")
        params.append(favorite)
    if tag is not None:
        conditions.append("EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id AND t.user_id = nt.user_id WHERE nt.note_id = n.id AND nt.user_id = %s AND t.name = %s)")
        params.extend([user_id, tag.strip().lower()])
    if q is not None and q.strip():
        conditions.append("(n.title ILIKE %s OR n.content ILIKE %s OR EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id AND t.user_id = nt.user_id WHERE nt.note_id = n.id AND nt.user_id = %s AND t.name ILIKE %s))")
        term = f"%{q.strip()}%"
        params.extend([term, term, user_id, term])
    order_sql = {"title": "n.title ASC", "updated_at": "n.updated_at DESC", "created_at": "n.created_at DESC"}[sort_by]
    params.extend([limit, offset])
    return fetch_notes_with_tags(
        conn,
        f"SELECT n.id, n.title, n.content, n.is_favorite, n.source_url, n.created_at, n.updated_at FROM notes n WHERE {' AND '.join(conditions)} ORDER BY {order_sql} LIMIT %s OFFSET %s",
        tuple(params), user_id,
    )


@app.get("/notes/favorites", response_model=list[NoteRead])
def list_favorite_notes(conn: Annotated[Connection, Depends(get_db)], current_user: CurrentUser) -> list[dict]:
    user_id = int(current_user["id"])
    return fetch_notes_with_tags(
        conn,
        """SELECT id, title, content, is_favorite, source_url, created_at, updated_at
           FROM notes WHERE user_id = %s AND is_favorite = TRUE ORDER BY created_at DESC""",
        (user_id,), user_id,
    )


@app.get("/tags")
def list_tags(conn: Annotated[Connection, Depends(get_db)], current_user: CurrentUser) -> list[dict]:
    user_id = int(current_user["id"])
    rows = conn.execute(
        """SELECT t.id, t.name, COUNT(nt.note_id)::int AS notes_count
           FROM tags t LEFT JOIN note_tags nt ON nt.tag_id = t.id AND nt.user_id = t.user_id
           WHERE t.user_id = %s
           GROUP BY t.id, t.name ORDER BY t.name""",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


@app.get("/tags/{tag_name}/notes", response_model=list[NoteRead])
def get_notes_by_tag(tag_name: str, conn: Annotated[Connection, Depends(get_db)], current_user: CurrentUser) -> list[dict]:
    user_id = int(current_user["id"])
    normalized_name = tag_name.strip().lower()
    exists = conn.execute(
        "SELECT 1 FROM tags WHERE name = %s AND user_id = %s", (normalized_name, user_id)
    ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return fetch_notes_with_tags(
        conn,
        """SELECT n.id, n.title, n.content, n.is_favorite, n.source_url, n.created_at, n.updated_at
           FROM notes n JOIN note_tags nt ON nt.note_id = n.id AND nt.user_id = n.user_id
           JOIN tags t ON t.id = nt.tag_id AND t.user_id = nt.user_id
           WHERE n.user_id = %s AND t.name = %s ORDER BY n.created_at DESC""",
        (user_id, normalized_name), user_id,
    )


@app.get("/notes/search", response_model=list[NoteRead])
def search_notes(
    q: Annotated[str, Query(min_length=1, description="Search notes by title or content")],
    conn: Annotated[Connection, Depends(get_db)],
    current_user: CurrentUser,
) -> list[dict]:
    user_id = int(current_user["id"])
    term = f"%{q}%"
    return fetch_notes_with_tags(
        conn,
        """SELECT n.id, n.title, n.content, n.is_favorite, n.source_url, n.created_at, n.updated_at
           FROM notes n WHERE n.user_id = %s AND (n.title ILIKE %s OR n.content ILIKE %s
           OR EXISTS (SELECT 1 FROM note_tags nt JOIN tags t ON t.id = nt.tag_id AND t.user_id = nt.user_id
                      WHERE nt.note_id = n.id AND nt.user_id = %s AND t.name ILIKE %s))
           ORDER BY n.created_at DESC""",
        (user_id, term, term, user_id, term), user_id,
    )


@app.get("/notes/{note_id}", response_model=NoteRead)
def get_note(note_id: int, conn: Annotated[Connection, Depends(get_db)], current_user: CurrentUser) -> dict:
    user_id = int(current_user["id"])
    note = fetch_note(conn, note_id, user_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note_to_dict(conn, note, user_id)


@app.post("/notes/{note_id}/relations", response_model=RelationRead, status_code=status.HTTP_201_CREATED)
def create_note_relation(
    note_id: int,
    relation_data: RelationCreate,
    conn: Annotated[Connection, Depends(get_db)],
    current_user: CurrentUser,
) -> dict:
    user_id = int(current_user["id"])
    with conn.transaction():
        if fetch_note(conn, note_id, user_id) is None:
            raise HTTPException(status_code=404, detail="Source note not found")
        if fetch_note(conn, relation_data.target_note_id, user_id) is None:
            raise HTTPException(status_code=404, detail="Target note not found")
        if note_id == relation_data.target_note_id:
            raise HTTPException(status_code=400, detail="A note cannot be related to itself")
        relation_type = relation_data.relation_type.strip().lower()
        if not relation_type:
            raise HTTPException(status_code=422, detail="Relation type cannot be empty")
        row = conn.execute(
            """INSERT INTO note_relations (user_id, source_note_id, target_note_id, relation_type)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (source_note_id, target_note_id, relation_type) DO NOTHING
               RETURNING id, source_note_id, target_note_id, relation_type, created_at""",
            (user_id, note_id, relation_data.target_note_id, relation_type),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="This relation already exists")
    return dict(row)


@app.get("/notes/{note_id}/relations", response_model=list[RelationRead])
def list_note_relations(note_id: int, conn: Annotated[Connection, Depends(get_db)], current_user: CurrentUser) -> list[dict]:
    user_id = int(current_user["id"])
    if fetch_note(conn, note_id, user_id) is None:
        raise HTTPException(status_code=404, detail="Note not found")
    rows = conn.execute(
        """SELECT id, source_note_id, target_note_id, relation_type, created_at
           FROM note_relations WHERE user_id = %s AND (source_note_id = %s OR target_note_id = %s)
           ORDER BY id""",
        (user_id, note_id, note_id),
    ).fetchall()
    return [dict(row) for row in rows]


@app.delete("/notes/{note_id}/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note_relation(
    note_id: int,
    relation_id: int,
    conn: Annotated[Connection, Depends(get_db)],
    current_user: CurrentUser,
) -> None:
    user_id = int(current_user["id"])
    with conn.transaction():
        row = conn.execute(
            """DELETE FROM note_relations WHERE id = %s AND user_id = %s
               AND (source_note_id = %s OR target_note_id = %s) RETURNING id""",
            (relation_id, user_id, note_id, note_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Relation not found")


@app.get("/notes/{note_id}/graph", response_model=GraphRead)
def get_note_graph(
    note_id: int,
    conn: Annotated[Connection, Depends(get_db)],
    current_user: CurrentUser,
    depth: int = Query(default=2, ge=1, le=5),
) -> GraphRead:
    user_id = int(current_user["id"])
    start_note = fetch_note(conn, note_id, user_id)
    if start_note is None:
        raise HTTPException(status_code=404, detail="Note not found")

    visited = {note_id}
    frontier = {note_id}
    found_notes: list[GraphNoteRead] = []
    found_relations: dict[int, dict] = {}
    for current_depth in range(depth):
        if not frontier:
            break
        frontier_ids = list(frontier)
        rows = conn.execute(
            """SELECT r.id, r.source_note_id, r.target_note_id, r.relation_type, r.created_at,
                      CASE WHEN r.source_note_id = ANY(%s) THEN r.target_note_id ELSE r.source_note_id END AS neighbor_id,
                      n.title AS neighbor_title
               FROM note_relations r
               JOIN notes n ON n.id = CASE WHEN r.source_note_id = ANY(%s)
                                           THEN r.target_note_id ELSE r.source_note_id END
                            AND n.user_id = %s
               WHERE r.user_id = %s AND (r.source_note_id = ANY(%s) OR r.target_note_id = ANY(%s))
               ORDER BY r.id""",
            (frontier_ids, frontier_ids, user_id, user_id, frontier_ids, frontier_ids),
        ).fetchall()
        next_frontier: set[int] = set()
        for row in rows:
            relation = {key: row[key] for key in ("id", "source_note_id", "target_note_id", "relation_type", "created_at")}
            found_relations[row["id"]] = relation
            neighbor_id = row["neighbor_id"]
            if neighbor_id not in visited:
                visited.add(neighbor_id)
                next_frontier.add(neighbor_id)
                found_notes.append(GraphNoteRead(id=neighbor_id, title=row["neighbor_title"], depth=current_depth + 1))
        frontier = next_frontier

    return GraphRead(
        start_note_id=note_id,
        depth=depth,
        notes=found_notes,
        relations=[RelationRead(**row) for row in found_relations.values()],
    )


@app.patch("/notes/{note_id}/favorite", response_model=NoteRead)
def toggle_note_favorite(note_id: int, conn: Annotated[Connection, Depends(get_db)], current_user: CurrentUser) -> dict:
    user_id = int(current_user["id"])
    with conn.transaction():
        row = conn.execute(
            """UPDATE notes SET is_favorite = NOT is_favorite
               WHERE id = %s AND user_id = %s
               RETURNING id, title, content, is_favorite, source_url, created_at, updated_at""",
            (note_id, user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Note not found")
        result = note_to_dict(conn, row, user_id)
    return result


@app.put("/notes/{note_id}", response_model=NoteRead)
def update_note(
    note_id: int,
    note_data: NoteUpdate,
    conn: Annotated[Connection, Depends(get_db)],
    current_user: CurrentUser,
) -> dict:
    user_id = int(current_user["id"])
    data = note_data.model_dump(exclude={"tags"})
    with conn.transaction():
        if fetch_note(conn, note_id, user_id) is None:
            raise HTTPException(status_code=404, detail="Note not found")
        row = conn.execute(
            """UPDATE notes SET title = %s, content = %s, source_url = %s, updated_at = now()
               WHERE id = %s AND user_id = %s
               RETURNING id, title, content, is_favorite, source_url, created_at, updated_at""",
            (data["title"], data["content"], data.get("source_url"), note_id, user_id),
        ).fetchone()
        set_note_tags(conn, user_id, note_id, note_data.tags)
        result = note_to_dict(conn, row, user_id)
    return result


@app.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(note_id: int, conn: Annotated[Connection, Depends(get_db)], current_user: CurrentUser) -> None:
    user_id = int(current_user["id"])
    with conn.transaction():
        row = conn.execute(
            "DELETE FROM notes WHERE id = %s AND user_id = %s RETURNING id", (note_id, user_id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Note not found")


app.mount(
    "/ui",
    StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True),
    name="ui",
)
