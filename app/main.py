from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import httpx

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
OAUTH_COOKIE_PREFIX = "recall_oauth_"
OAUTH_FLOW_TTL_SECONDS = 600
OAUTH_PROVIDERS = {"github", "yandex"}
DEFAULT_SESSION_TTL = 8 * 60 * 60
PASSWORD_ITERATIONS = 600_000
LOGIN_FAILURES: dict[str, tuple[int, float]] = {}
REGISTER_FAILURES: dict[str, tuple[int, float]] = {}
RESET_FAILURES: dict[str, tuple[int, float]] = {}
logger = logging.getLogger("recall.auth")


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class EmailRequest(BaseModel):
    email: EmailStr


class PasswordResetRequest(BaseModel):
    token: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]{32,128}$")
    password: str = Field(min_length=12, max_length=128)


class EmailRequestToken(BaseModel):
    token: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]{32,128}$")


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


def _make_session(user_id: int, secret: bytes, ttl: int, session_version: int = 0) -> str:
    payload = {
        "sub": int(user_id),
        "sv": int(session_version),
        "exp": int(time.time()) + ttl,
        "nonce": secrets.token_urlsafe(16),
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + _b64url_encode(signature)


def _session_payload(token: str | None) -> dict | None:
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
        payload["sub"] = user_id
        payload["sv"] = int(payload.get("sv", 0))
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _session_user_id(token: str | None) -> int | None:
    payload = _session_payload(token)
    return int(payload["sub"]) if payload else None


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


def _set_session_cookie(response: Response, request: Request, user_id: int, session_version: int = 0) -> None:
    config = _session_secret()
    if config is None:
        raise HTTPException(status_code=503, detail="RECALL_SECRET_KEY is missing or invalid")
    secret, ttl = config
    response.set_cookie(
        key=SESSION_COOKIE,
        value=_make_session(user_id, secret, ttl, session_version),
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


def _public_url(path: str) -> str:
    base = os.getenv("RECALL_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="Email links are not configured: set RECALL_PUBLIC_BASE_URL.")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(status_code=503, detail="RECALL_PUBLIC_BASE_URL is invalid.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise HTTPException(status_code=503, detail="RECALL_PUBLIC_BASE_URL must use HTTPS.")
    return base + path


def _oauth_provider_config(provider: str) -> tuple[str, str, str]:
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail="Неизвестный OAuth-провайдер.")
    prefix = "RECALL_GITHUB" if provider == "github" else "RECALL_YANDEX"
    client_id = os.getenv(prefix + "_CLIENT_ID", "").strip()
    client_secret = os.getenv(prefix + "_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail=f"Вход через {provider.title()} ещё не настроен. Добавь OAuth Client ID и Client Secret в .env.",
        )
    redirect_uri = _public_url(f"/auth/oauth/{provider}/callback")
    return client_id, client_secret, redirect_uri


def _oauth_cookie_name(provider: str) -> str:
    return OAUTH_COOKIE_PREFIX + provider


def _make_oauth_flow_cookie(
    provider: str,
    state: str,
    verifier: str,
    redirect_uri: str,
    *,
    mode: str,
    user_id: int | None = None,
    session_version: int | None = None,
) -> str:
    config = _session_secret()
    if config is None:
        raise HTTPException(status_code=503, detail="RECALL_SECRET_KEY is missing or invalid")
    secret, _ttl = config
    payload = {
        "provider": provider,
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "mode": mode,
        "user_id": int(user_id) if user_id is not None else None,
        "session_version": int(session_version) if session_version is not None else None,
        "exp": int(time.time()) + OAUTH_FLOW_TTL_SECONDS,
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret, f"oauth:{provider}:{body}".encode("ascii"), hashlib.sha256).digest()
    return body + "." + _b64url_encode(signature)


def _read_oauth_flow_cookie(provider: str, cookie: str | None, supplied_state: str | None) -> dict | None:
    config = _session_secret()
    if not cookie or not supplied_state or config is None:
        return None
    secret, _ttl = config
    try:
        body, supplied_signature = cookie.split(".", 1)
        expected = hmac.new(secret, f"oauth:{provider}:{body}".encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(supplied_signature)):
            return None
        payload = json.loads(_b64url_decode(body))
        if payload.get("provider") != provider or payload.get("mode") not in {"login", "link"}:
            return None
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
        if not hmac.compare_digest(str(payload.get("state", "")), supplied_state):
            return None
        if not payload.get("verifier") or not payload.get("redirect_uri"):
            return None
        if payload.get("mode") == "link" and not payload.get("user_id"):
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _oauth_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = _b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _start_oauth(
    provider: str,
    request: Request,
    *,
    mode: str,
    user_id: int | None = None,
    session_version: int | None = None,
) -> RedirectResponse:
    client_id, _client_secret, redirect_uri = _oauth_provider_config(provider)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _oauth_pkce_pair()
    common = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if provider == "github":
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = "https://github.com/login/oauth/authorize?" + urlencode(query)
    else:
        query = {
            **common,
            "scope": "login:info login:email",
            "force_confirm": "yes",
        }
        authorize_url = "https://oauth.yandex.ru/authorize?" + urlencode(query)

    response = RedirectResponse(authorize_url, status_code=302)
    response.set_cookie(
        key=_oauth_cookie_name(provider),
        value=_make_oauth_flow_cookie(
            provider, state, verifier, redirect_uri, mode=mode,
            user_id=user_id, session_version=session_version,
        ),
        max_age=OAUTH_FLOW_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return response


def _clear_oauth_cookie(response: Response, request: Request, provider: str) -> None:
    response.delete_cookie(
        key=_oauth_cookie_name(provider),
        path="/",
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
    )


def _oauth_identity(provider: str, code: str, flow: dict) -> dict[str, str | None]:
    client_id, client_secret, _configured_redirect_uri = _oauth_provider_config(provider)
    redirect_uri = str(flow["redirect_uri"])
    verifier = str(flow["verifier"])

    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            if provider == "github":
                token_response = client.post(
                    "https://github.com/login/oauth/access_token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "code_verifier": verifier,
                    },
                    headers={"Accept": "application/json"},
                )
                token_response.raise_for_status()
                token_data = token_response.json()
                access_token = token_data.get("access_token")
                if not access_token or token_data.get("error"):
                    raise ValueError("GitHub не выдал токен доступа.")
                headers = {
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
                user_response = client.get("https://api.github.com/user", headers=headers)
                user_response.raise_for_status()
                profile = user_response.json()
                emails_response = client.get("https://api.github.com/user/emails", headers=headers)
                emails_response.raise_for_status()
                emails = emails_response.json()
                primary_email = next(
                    (item.get("email") for item in emails if item.get("primary") and item.get("verified")),
                    None,
                )
                subject = str(profile.get("id", "")).strip()
                display_name = str(profile.get("login") or "GitHub user")[:120]
                email = str(primary_email).strip().lower() if primary_email else None
            else:
                token_response = client.post(
                    "https://oauth.yandex.ru/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "redirect_uri": redirect_uri,
                        "code_verifier": verifier,
                    },
                    headers={"Accept": "application/json"},
                )
                token_response.raise_for_status()
                token_data = token_response.json()
                access_token = token_data.get("access_token")
                if not access_token or token_data.get("error"):
                    raise ValueError("Яндекс не выдал токен доступа.")
                info_response = client.get(
                    "https://login.yandex.ru/info",
                    params={"format": "json"},
                    headers={"Authorization": f"OAuth {access_token}"},
                )
                info_response.raise_for_status()
                profile = info_response.json()
                subject = str(profile.get("id", "")).strip()
                display_name = str(profile.get("display_name") or profile.get("login") or "Yandex user")[:120]
                raw_email = profile.get("default_email")
                if not raw_email and profile.get("emails"):
                    raw_email = profile["emails"][0]
                email = str(raw_email).strip().lower() if raw_email else None

    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("OAuth identity lookup failed for %s (%s)", provider, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Не удалось получить подтверждение от OAuth-провайдера. Попробуй ещё раз.") from exc

    if not subject:
        raise HTTPException(status_code=502, detail="OAuth-провайдер не вернул идентификатор аккаунта.")
    return {
        "provider_user_id": subject,
        "display_name": display_name,
        "email": email,
    }


def _send_email(recipient: str, subject: str, body: str) -> None:
    host = os.getenv("RECALL_SMTP_HOST", "").strip()
    sender = os.getenv("RECALL_EMAIL_FROM", "").strip()
    if not host or not sender:
        raise HTTPException(status_code=503, detail="Email is not configured. Set RECALL_SMTP_HOST and RECALL_EMAIL_FROM in .env.")

    port = int(os.getenv("RECALL_SMTP_PORT", "587"))
    username = os.getenv("RECALL_SMTP_USERNAME", "").strip()
    password = os.getenv("RECALL_SMTP_PASSWORD", "")
    use_ssl = os.getenv("RECALL_SMTP_USE_SSL", "0").strip().lower() in {"1", "true", "yes"}
    use_starttls = os.getenv("RECALL_SMTP_STARTTLS", "1").strip().lower() in {"1", "true", "yes"}
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.ehlo()
                if use_starttls:
                    smtp.starttls()
                    smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise HTTPException(status_code=503, detail="Не удалось отправить письмо. Проверь настройки почты.") from exc


def _validate_email_configuration() -> None:
    if not os.getenv("RECALL_SMTP_HOST", "").strip() or not os.getenv("RECALL_EMAIL_FROM", "").strip():
        raise HTTPException(status_code=503, detail="Email is not configured. Set RECALL_SMTP_HOST and RECALL_EMAIL_FROM in .env.")
    _public_url("/health")
    try:
        port = int(os.getenv("RECALL_SMTP_PORT", "587"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="RECALL_SMTP_PORT is invalid.") from exc


def _issue_auth_token(
    conn: Connection, *, purpose: str, user_id: int | None = None, email: str | None = None
) -> tuple[int, str, str] | None:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    ttl = 30 * 60 if purpose == "password_reset" else 24 * 60 * 60
    row = conn.execute(
        "SELECT * FROM public.recall_issue_auth_token(%s, %s, %s, %s, now() + (%s * interval '1 second'))",
        (user_id, email, purpose, token_hash, ttl),
    ).fetchone()
    if row is None:
        return None
    return int(row["user_id"]), str(row["email"]), token


LOGIN_HTML = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Вход — Recall</title>
<style>
:root{color-scheme:dark;--bg:#080a12;--panel:rgba(18,22,38,.9);--line:rgba(169,181,219,.17);--text:#f2f4ff;--muted:#929bb5;--violet:#a18aff;--cyan:#65d9ed}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(ellipse at 14% 12%,#7650ce35,transparent 38%),radial-gradient(ellipse at 88% 85%,#2697b42a,transparent 35%),var(--bg);font:15px/1.55 Inter,Segoe UI,Arial,sans-serif;color:var(--text)}.card{width:min(100%,430px);padding:38px;border:1px solid var(--line);border-radius:25px;background:var(--panel);box-shadow:0 28px 100px #0009;backdrop-filter:blur(22px)}.mark{width:48px;height:48px;display:grid;place-items:center;border-radius:16px;background:linear-gradient(135deg,#a18aff,#65d9ed);font-size:23px;font-weight:900;color:#090a12}h1{margin:24px 0 4px;font-size:30px;letter-spacing:-1.2px}p{margin:0 0 25px;color:var(--muted)}label{display:block;margin:16px 0 7px;font-size:13px;color:#c9cfe3}input{width:100%;padding:13px 14px;border:1px solid var(--line);border-radius:12px;background:#0b0e19;color:var(--text);font:inherit;outline:none}input:focus{border-color:var(--violet);box-shadow:0 0 0 3px #a18aff20}button{width:100%;margin-top:23px;padding:13px 16px;border:0;border-radius:12px;background:linear-gradient(110deg,#a18aff,#65d9ed);color:#10101b;font-weight:800;font-size:15px;cursor:pointer}button:disabled{opacity:.65;cursor:wait}#error{min-height:20px;margin-top:14px;color:#fda4af;font-size:13px}.foot{margin-top:22px;color:#8d92a9;font-size:13px;text-align:center}.foot a{color:#c4b5fd;text-decoration:none}.oauth-separator{display:flex;align-items:center;gap:12px;margin:19px 0 12px;color:#858da8;font-size:12px}.oauth-separator:before,.oauth-separator:after{content:"";height:1px;flex:1;background:var(--line)}.oauth-buttons{display:grid;grid-template-columns:1fr 1fr;gap:10px}.oauth-button{display:flex;align-items:center;justify-content:center;gap:9px;min-height:44px;padding:10px 12px;border:1px solid var(--line);border-radius:12px;text-decoration:none;font-size:13px;font-weight:700;transition:.2s}.oauth-button:hover{transform:translateY(-1px);border-color:#a18aff80}.oauth-button.github{color:#f2f4ff;background:#171b2a}.oauth-button.yandex{color:#211900;background:#ffdb4d;border-color:#ffdb4d}.oauth-icon{display:grid;place-items:center;width:23px;height:23px;border-radius:7px;background:#ffffff18;font-size:11px;font-weight:900}.oauth-button.yandex .oauth-icon{background:#241d0018}
</style></head><body><main class="card"><div class="mark">R</div><h1>С возвращением</h1><p>Войди, чтобы открыть свою базу знаний.</p><form id="login-form"><label for="identifier">Логин или email</label><input id="identifier" name="identifier" autocomplete="username" required maxlength="254"><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="128"><button id="submit" type="submit">Войти в Recall</button><div id="error" role="alert"></div></form><div class="oauth-separator">или продолжить с помощью</div><div class="oauth-buttons"><a class="oauth-button github" href="/auth/oauth/github/start"><span class="oauth-icon">GH</span><span>GitHub</span></a><a class="oauth-button yandex" href="/auth/oauth/yandex/start"><span class="oauth-icon">Я</span><span>Яндекс</span></a></div><div class="foot"><a href="/forgot-password">Забыли пароль?</a></div><div class="foot">Впервые здесь? <a href="/register">Создать аккаунт</a></div></main>
<script>const form=document.querySelector('#login-form'),error=document.querySelector('#error'),button=document.querySelector('#submit');const oauthMessages={not_linked:'Этот аккаунт ещё не подключён к Recall. Войди с паролем и подключи его в разделе «Безопасность».',denied:'Вход отменён на стороне провайдера.',invalid:'Не удалось проверить OAuth-сессию. Начни вход заново.',provider_error:'Провайдер не подтвердил вход. Попробуй ещё раз.',session_expired:'Сессия Recall истекла. Войди паролем и повтори подключение.'};const oauthResult=new URLSearchParams(location.search).get('oauth');if(oauthResult&&oauthMessages[oauthResult])error.textContent=oauthMessages[oauthResult];form.addEventListener('submit',async e=>{e.preventDefault();error.textContent='';button.disabled=true;button.textContent='Проверяем...';try{const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({identifier:form.identifier.value.trim(),password:form.password.value})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'Не удалось войти');location.replace('/ui/')}catch(err){error.textContent=err.message||'Ошибка соединения'}finally{button.disabled=false;button.textContent='Войти в Recall'}});</script></body></html>'''

REGISTER_HTML = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Регистрация — Recall</title>
<style>
:root{color-scheme:dark;--bg:#080a12;--panel:rgba(18,22,38,.9);--line:rgba(169,181,219,.17);--text:#f2f4ff;--muted:#929bb5;--violet:#a18aff;--cyan:#65d9ed}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(ellipse at 14% 12%,#7650ce35,transparent 38%),radial-gradient(ellipse at 88% 85%,#2697b42a,transparent 35%),var(--bg);font:15px/1.55 Inter,Segoe UI,Arial,sans-serif;color:var(--text)}.card{width:min(100%,450px);padding:38px;border:1px solid var(--line);border-radius:25px;background:var(--panel);box-shadow:0 28px 100px #0009;backdrop-filter:blur(22px)}.mark{width:48px;height:48px;display:grid;place-items:center;border-radius:16px;background:linear-gradient(135deg,#a18aff,#65d9ed);font-size:23px;font-weight:900;color:#090a12}h1{margin:24px 0 4px;font-size:30px;letter-spacing:-1.2px}p{margin:0 0 22px;color:var(--muted)}label{display:block;margin:14px 0 7px;font-size:13px;color:#c9cfe3}input{width:100%;padding:13px 14px;border:1px solid var(--line);border-radius:12px;background:#0b0e19;color:var(--text);font:inherit;outline:none}input:focus{border-color:var(--violet);box-shadow:0 0 0 3px #a18aff20}button{width:100%;margin-top:22px;padding:13px 16px;border:0;border-radius:12px;background:linear-gradient(110deg,#a18aff,#65d9ed);color:#10101b;font-weight:800;font-size:15px;cursor:pointer}button:disabled{opacity:.65;cursor:wait}#error{min-height:20px;margin-top:14px;color:#fda4af;font-size:13px}.hint{margin-top:8px;color:#858da8;font-size:11px}.foot{margin-top:20px;color:#8d92a9;font-size:13px;text-align:center}.foot a{color:#c4b5fd;text-decoration:none}
</style></head><body><main class="card"><div class="mark">R</div><h1>Создай свой Recall</h1><p>Личное пространство для заметок, идей и связей.</p><form id="register-form"><label for="username">Имя пользователя</label><input id="username" name="username" autocomplete="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9_.-]+" required><div class="hint">3–32 символа: латинские буквы, цифры, точка, дефис или подчёркивание.</div><label for="email">Email</label><input id="email" name="email" type="email" autocomplete="email" maxlength="254" required><label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required><div class="hint">Не менее 12 символов.</div><label for="confirm">Повтори пароль</label><input id="confirm" name="confirm" type="password" autocomplete="new-password" minlength="12" maxlength="128" required><button id="submit" type="submit">Создать аккаунт</button><div id="error" role="alert"></div></form><div class="foot">Уже есть аккаунт? <a href="/login">Войти</a></div></main>
<script>const form=document.querySelector('#register-form'),error=document.querySelector('#error'),button=document.querySelector('#submit');form.addEventListener('submit',async e=>{e.preventDefault();error.textContent='';if(form.password.value!==form.confirm.value){error.textContent='Пароли не совпадают.';return}button.disabled=true;button.textContent='Создаём аккаунт...';try{const r=await fetch('/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:form.username.value.trim(),email:form.email.value.trim(),password:form.password.value})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'Не удалось создать аккаунт');location.replace('/ui/')}catch(err){error.textContent=err.message||'Ошибка соединения'}finally{button.disabled=false;button.textContent='Создать аккаунт'}});</script></body></html>'''

FORGOT_PASSWORD_HTML = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Восстановление пароля — Recall</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#080a12;color:#f2f4ff;font:15px/1.55 Segoe UI,Arial,sans-serif}.card{width:min(100%,430px);padding:36px;border:1px solid #30364d;border-radius:24px;background:#121626}h1{margin:0 0 8px}p{color:#929bb5}label{display:block;margin:18px 0 7px}input{width:100%;box-sizing:border-box;padding:13px;border:1px solid #39405a;border-radius:11px;background:#0b0e19;color:white}button{width:100%;margin-top:20px;padding:13px;border:0;border-radius:11px;background:linear-gradient(110deg,#a18aff,#65d9ed);font-weight:800;cursor:pointer}#msg{min-height:42px;margin-top:14px;color:#c4b5fd}.back{color:#c4b5fd;text-decoration:none}</style></head><body><main class="card"><h1>Восстановление пароля</h1><p>Укажи email, указанный при регистрации. Мы отправим ссылку, если аккаунт существует.</p><form id="f"><label for="email">Email</label><input id="email" type="email" autocomplete="email" required maxlength="254"><button>Отправить ссылку</button><div id="msg" role="status"></div></form><a class="back" href="/login">Вернуться ко входу</a></main><script>const f=document.querySelector('#f'),m=document.querySelector('#msg');f.addEventListener('submit',async e=>{e.preventDefault();m.textContent='Отправляем...';try{const r=await fetch('/auth/password/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:document.querySelector('#email').value.trim()})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'Ошибка отправки');m.textContent=d.message||'Если аккаунт с таким email существует, письмо отправлено.'}catch(e){m.textContent=e.message||'Ошибка соединения'}});</script></body></html>'''

RESET_PASSWORD_HTML = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Новый пароль — Recall</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#080a12;color:#f2f4ff;font:15px/1.55 Segoe UI,Arial,sans-serif}.card{width:min(100%,430px);padding:36px;border:1px solid #30364d;border-radius:24px;background:#121626}h1{margin:0 0 8px}p{color:#929bb5}label{display:block;margin:18px 0 7px}input{width:100%;box-sizing:border-box;padding:13px;border:1px solid #39405a;border-radius:11px;background:#0b0e19;color:white}button{width:100%;margin-top:20px;padding:13px;border:0;border-radius:11px;background:linear-gradient(110deg,#a18aff,#65d9ed);font-weight:800;cursor:pointer}#msg{min-height:42px;margin-top:14px;color:#c4b5fd}</style></head><body><main class="card"><h1>Задай новый пароль</h1><p>Используй не менее 12 символов.</p><form id="f"><label for="password">Новый пароль</label><input id="password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required><label for="confirm">Повтори пароль</label><input id="confirm" type="password" autocomplete="new-password" minlength="12" maxlength="128" required><button>Сохранить новый пароль</button><div id="msg" role="status"></div></form></main><script>const f=document.querySelector('#f'),m=document.querySelector('#msg'),token=new URLSearchParams(location.search).get('token')||'';f.addEventListener('submit',async e=>{e.preventDefault();const p=document.querySelector('#password').value;if(p!==document.querySelector('#confirm').value){m.textContent='Пароли не совпадают.';return}try{const r=await fetch('/auth/password/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,password:p})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'Ссылка недействительна или устарела');m.textContent='Пароль изменён. Перенаправляем на страницу входа...';setTimeout(()=>location.replace('/login'),1200)}catch(e){m.textContent=e.message||'Ошибка соединения'}});</script></body></html>'''

VERIFY_EMAIL_HTML = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Подтверждение email — Recall</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#080a12;color:#f2f4ff;font:15px/1.55 Segoe UI,Arial,sans-serif}.card{width:min(100%,430px);padding:36px;border:1px solid #30364d;border-radius:24px;background:#121626}p{color:#929bb5}button{width:100%;margin-top:20px;padding:13px;border:0;border-radius:11px;background:linear-gradient(110deg,#a18aff,#65d9ed);font-weight:800;cursor:pointer}#msg{color:#c4b5fd}</style></head><body><main class="card"><h1>Подтверждение email</h1><p>Нажми кнопку ниже, чтобы подтвердить адрес электронной почты.</p><button id="confirm">Подтвердить email</button><p id="msg" role="status"></p></main><script>const token=new URLSearchParams(location.search).get('token')||'',b=document.querySelector('#confirm'),m=document.querySelector('#msg');b.addEventListener('click',async()=>{b.disabled=true;try{const r=await fetch('/auth/email/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'Ссылка недействительна или устарела');m.textContent='Email подтверждён. Теперь можно закрыть страницу.';b.hidden=true}catch(e){m.textContent=e.message||'Ошибка соединения';b.disabled=false}});</script></body></html>'''

ACCOUNT_SECURITY_HTML = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Безопасность — Recall</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:#080a12;color:#f2f4ff;font:15px/1.55 Segoe UI,Arial,sans-serif}.card{width:min(100%,500px);padding:36px;border:1px solid #30364d;border-radius:24px;background:#121626;box-shadow:0 24px 70px #0008}p{color:#929bb5}.status{color:#c4b5fd}.provider{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:12px 0;padding:14px;border:1px solid #30364d;border-radius:13px;background:#0b0e19}.provider strong{display:block}.provider small{color:#929bb5}.provider-action{display:inline-flex;align-items:center;justify-content:center;padding:9px 12px;border:1px solid #555071;border-radius:10px;color:#d7ccff;text-decoration:none;font-size:12px;font-weight:700;white-space:nowrap}.provider-action.connected{border-color:#286a58;color:#86e0be;background:#10251f;cursor:default}.provider-form{margin:0}.provider-form .provider-action{width:auto;margin:0;padding:9px 12px;background:transparent;font:700 12px Segoe UI,Arial,sans-serif;cursor:pointer}button{width:100%;margin-top:18px;padding:13px;border:0;border-radius:11px;background:linear-gradient(110deg,#a18aff,#65d9ed);font-weight:800;cursor:pointer}a{color:#c4b5fd;text-decoration:none}.section-title{margin:26px 0 8px;font-size:16px}#msg{min-height:22px;color:#c4b5fd;font-size:13px}</style></head><body><main class="card"><h1>Безопасность аккаунта</h1><p>Email: <strong>__EMAIL__</strong></p><p class="status">Статус: __STATUS__</p><button id="send" __DISABLED__>Отправить письмо для подтверждения</button><p id="msg" role="status"></p><h2 class="section-title">Способы входа</h2><div class="provider"><div><strong>GitHub</strong><small>__GITHUB_STATUS__</small></div>__GITHUB_ACTION__</div><div class="provider"><div><strong>Яндекс</strong><small>__YANDEX_STATUS__</small></div>__YANDEX_ACTION__</div><p><a href="/ui/">Вернуться в Recall</a></p></main><script>const b=document.querySelector('#send'),m=document.querySelector('#msg');b.addEventListener('click',async()=>{b.disabled=true;m.textContent='Отправляем...';try{const r=await fetch('/auth/email/verification/request',{method:'POST'});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'Не удалось отправить письмо');m.textContent=d.message||'Письмо отправлено.'}catch(e){m.textContent=e.message||'Ошибка соединения';b.disabled=false}});const oauthMessages={linked:'Способ входа успешно подключён.',already_linked:'Этот способ входа уже подключён к аккаунту.',identity_in_use:'Этот внешний аккаунт уже привязан к другому аккаунту Recall.',provider_already_linked:'К аккаунту уже подключён другой аккаунт этого провайдера.',denied:'Подключение отменено.',invalid:'Не удалось проверить OAuth-сессию. Повтори подключение.',provider_error:'Не удалось подтвердить аккаунт у провайдера.',session_expired:'Сессия Recall истекла. Войди снова и повтори подключение.'};const result=new URLSearchParams(location.search).get('oauth');if(result&&oauthMessages[result])m.textContent=oauthMessages[result];</script></body></html>'''


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

    public_paths = {
        "/login", "/register", "/auth/login", "/auth/register", "/logout", "/health",
        "/forgot-password", "/reset-password", "/verify-email",
        "/auth/password/request", "/auth/password/reset", "/auth/email/verify",
        "/auth/oauth/github/start", "/auth/oauth/yandex/start",
        "/auth/oauth/github/callback", "/auth/oauth/yandex/callback",
    }
    if path in public_paths:
        return await call_next(request)

    secret_config = _session_secret()
    if secret_config is None:
        return JSONResponse(
            {"detail": "Authentication is not configured. Set RECALL_SECRET_KEY in .env."},
            status_code=503,
        )

    session_payload = _session_payload(request.cookies.get(SESSION_COOKIE))
    user_id = int(session_payload["sub"]) if session_payload else None
    if user_id is None:
        if request.method == "GET" and (path == "/" or path.startswith("/ui") or path in {"/docs", "/redoc", "/openapi.json"}):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Authentication required"}, status_code=401)

    request.state.auth_user_id = user_id
    request.state.auth_session_version = int(session_payload.get("sv", 0))
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
        "SELECT id, username, email, email_verified_at, session_version FROM users WHERE id = %s AND is_active = TRUE",
        (user_id,),
    ).fetchone()
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    if os.getenv("RECALL_TESTING") != "1" and int(user["session_version"]) != int(getattr(request.state, "auth_session_version", 0)):
        raise HTTPException(status_code=401, detail="Сессия завершена. Войди снова.")
    return dict(user)


CurrentUser = Annotated[dict, Depends(get_current_user)]


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML)


@app.get("/register", response_class=HTMLResponse)
def register_page() -> HTMLResponse:
    return HTMLResponse(REGISTER_HTML)


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page() -> HTMLResponse:
    return HTMLResponse(FORGOT_PASSWORD_HTML)


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page() -> HTMLResponse:
    return HTMLResponse(
        RESET_PASSWORD_HTML,
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
    )


@app.get("/verify-email", response_class=HTMLResponse)
def verify_email_page() -> HTMLResponse:
    return HTMLResponse(
        VERIFY_EMAIL_HTML,
        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
    )


@app.get("/account/security", response_class=HTMLResponse)
def account_security_page(
    current_user: CurrentUser,
    conn: Annotated[Connection, Depends(get_db)],
) -> HTMLResponse:
    def escape_html(value: str) -> str:
        return (value.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#x27;"))

    email = escape_html(str(current_user["email"] or "не указан"))
    verified = current_user.get("email_verified_at") is not None
    conn.execute("SELECT set_config('app.user_id', %s, false)", (str(current_user["id"]),))
    linked_rows = conn.execute("SELECT * FROM public.recall_list_oauth_accounts()").fetchall()
    linked = {row["provider"]: row for row in linked_rows}
    html = ACCOUNT_SECURITY_HTML.replace("__EMAIL__", email)
    html = html.replace("__STATUS__", "подтверждён" if verified else "не подтверждён")
    html = html.replace("__DISABLED__", "disabled" if verified else "")
    for provider, title in (("github", "GitHub"), ("yandex", "Яндекс")):
        row = linked.get(provider)
        if row:
            details = escape_html(str(row.get("provider_username") or row.get("provider_email") or "аккаунт подключён"))
            status_text = "Подключён · " + details
            action = '<span class="provider-action connected">Подключён</span>'
        else:
            status_text = "Не подключён"
            action = f'<form class="provider-form" method="post" action="/auth/oauth/{provider}/link"><button class="provider-action" type="submit">Подключить</button></form>'
        html = html.replace(f"__{provider.upper()}_STATUS__", status_text)
        html = html.replace(f"__{provider.upper()}_ACTION__", action)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/auth/oauth/{provider}/start")
def oauth_login_start(provider: str, request: Request) -> RedirectResponse:
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail="Неизвестный OAuth-провайдер.")
    return _start_oauth(provider, request, mode="login")


@app.post("/auth/oauth/{provider}/link")
def oauth_link_start(
    provider: str,
    request: Request,
    current_user: CurrentUser,
) -> RedirectResponse:
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail="Неизвестный OAuth-провайдер.")
    return _start_oauth(
        provider,
        request,
        mode="link",
        user_id=int(current_user["id"]),
        session_version=int(current_user.get("session_version", 0)),
    )


@app.get("/auth/oauth/{provider}/callback")
def oauth_callback(
    provider: str,
    request: Request,
    conn: Annotated[Connection, Depends(get_db)],
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=1024),
    error: str | None = Query(default=None, max_length=128),
) -> RedirectResponse:
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail="Неизвестный OAuth-провайдер.")

    flow = _read_oauth_flow_cookie(
        provider, request.cookies.get(_oauth_cookie_name(provider)), state
    )
    if flow is None:
        response = RedirectResponse("/login?oauth=invalid", status_code=303)
        _clear_oauth_cookie(response, request, provider)
        return response

    destination = "/account/security" if flow["mode"] == "link" else "/login"
    if error or not code:
        response = RedirectResponse(destination + "?oauth=denied", status_code=303)
        _clear_oauth_cookie(response, request, provider)
        return response

    try:
        identity = _oauth_identity(provider, code, flow)
    except HTTPException:
        response = RedirectResponse(destination + "?oauth=provider_error", status_code=303)
        _clear_oauth_cookie(response, request, provider)
        return response

    if flow["mode"] == "link":
        session_payload = _session_payload(request.cookies.get(SESSION_COOKIE))
        expected_user_id = int(flow["user_id"])
        expected_version = int(flow.get("session_version") or 0)
        if (
            session_payload is None
            or int(session_payload.get("sub", 0)) != expected_user_id
            or int(session_payload.get("sv", 0)) != expected_version
        ):
            response = RedirectResponse("/login?oauth=session_expired", status_code=303)
            _clear_oauth_cookie(response, request, provider)
            return response

        conn.execute("SELECT set_config('app.user_id', %s, false)", (str(expected_user_id),))
        active_user = conn.execute(
            "SELECT id, session_version FROM users WHERE id = %s AND is_active = TRUE",
            (expected_user_id,),
        ).fetchone()
        if active_user is None or int(active_user["session_version"]) != expected_version:
            response = RedirectResponse("/login?oauth=session_expired", status_code=303)
            _clear_oauth_cookie(response, request, provider)
            return response

        result_row = conn.execute(
            "SELECT public.recall_link_oauth_account(%s, %s, %s, %s, %s) AS result",
            (
                expected_user_id,
                provider,
                identity["provider_user_id"],
                identity["display_name"],
                identity["email"],
            ),
        ).fetchone()
        result = str(result_row["result"] if result_row else "conflict")
        allowed = {"linked", "already_linked", "identity_in_use", "provider_already_linked"}
        if result not in allowed:
            result = "invalid"
        response = RedirectResponse("/account/security?oauth=" + result, status_code=303)
        _clear_oauth_cookie(response, request, provider)
        return response

    linked_user = conn.execute(
        "SELECT * FROM public.recall_find_oauth_user(%s, %s)",
        (provider, identity["provider_user_id"]),
    ).fetchone()
    if linked_user is None:
        response = RedirectResponse("/login?oauth=not_linked", status_code=303)
        _clear_oauth_cookie(response, request, provider)
        return response

    response = RedirectResponse("/ui/", status_code=303)
    _set_session_cookie(
        response, request, int(linked_user["user_id"]), int(linked_user["session_version"])
    )
    _clear_oauth_cookie(response, request, provider)
    return response


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
    # Sending is optional during initial local setup. Once SMTP is configured,
    # new accounts receive confirmation automatically; the security page lets
    # an existing account request the message again.
    if (
        os.getenv("RECALL_SMTP_HOST", "").strip()
        and os.getenv("RECALL_EMAIL_FROM", "").strip()
        and os.getenv("RECALL_PUBLIC_BASE_URL", "").strip()
    ):
        try:
            issued = _issue_auth_token(conn, user_id=int(row["id"]), purpose="verify_email")
            if issued is not None:
                _user_id, address, token = issued
                link = _public_url("/verify-email?token=" + token)
                _send_email(
                    address,
                    "Подтверждение email в Recall",
                    "Чтобы подтвердить адрес электронной почты, откройте ссылку в течение 24 часов:\n\n"
                    + link,
                )
        except HTTPException:
            logger.exception("Initial email verification message could not be sent")
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
    _set_session_cookie(response, request, int(user["id"]), int(user.get("session_version", 0)))
    return {"status": "ok", "username": user["username"]}


@app.post("/auth/password/request")
def request_password_reset(
    payload: EmailRequest,
    request: Request,
    conn: Annotated[Connection, Depends(get_db)],
) -> dict[str, str]:
    _rate_limit(RESET_FAILURES, _client_key(request), limit=5)
    # Check global mail configuration before looking up the address, so the
    # response does not disclose whether an account exists.
    _validate_email_configuration()
    email = str(payload.email).strip().lower()
    issued = _issue_auth_token(conn, email=email, purpose="password_reset")
    if issued is not None:
        _user_id, address, token = issued
        link = _public_url("/reset-password?token=" + token)
        try:
            _send_email(
                address,
                "Восстановление пароля Recall",
                "Получен запрос на смену пароля Recall. Если это были вы, откройте ссылку в течение 30 минут:\n\n"
                + link
                + "\n\nЕсли вы не запрашивали смену пароля, просто проигнорируйте письмо.",
            )
        except HTTPException:
            logger.exception("Password recovery email could not be sent")
    return {"status": "ok", "message": "Если аккаунт с таким email существует, письмо с инструкцией отправлено."}


@app.post("/auth/password/reset")
def reset_password(
    payload: PasswordResetRequest,
    request: Request,
    conn: Annotated[Connection, Depends(get_db)],
) -> dict[str, str]:
    _rate_limit(RESET_FAILURES, f"reset:{_client_key(request)}", limit=5)
    token_hash = hashlib.sha256(payload.token.encode("ascii", errors="ignore")).hexdigest()
    password_hash = _hash_password(payload.password)
    row = conn.execute(
        "SELECT public.recall_consume_password_reset(%s, %s) AS consumed",
        (token_hash, password_hash),
    ).fetchone()
    if not row or not row["consumed"]:
        raise HTTPException(status_code=400, detail="Ссылка недействительна, уже использована или устарела.")
    return {"status": "ok", "message": "Пароль изменён. Войдите с новым паролем."}


@app.post("/auth/email/verification/request")
def request_email_verification(
    request: Request,
    conn: Annotated[Connection, Depends(get_db)],
    current_user: CurrentUser,
) -> dict[str, str]:
    _rate_limit(RESET_FAILURES, f"verify:{current_user['id']}:{_client_key(request)}", limit=5)
    if current_user.get("email_verified_at") is not None:
        return {"status": "ok", "message": "Email уже подтверждён."}
    _validate_email_configuration()
    issued = _issue_auth_token(conn, user_id=int(current_user["id"]), purpose="verify_email")
    if issued is None:
        raise HTTPException(status_code=400, detail="У аккаунта нет email для подтверждения.")
    _user_id, address, token = issued
    link = _public_url("/verify-email?token=" + token)
    _send_email(
        address,
        "Подтверждение email в Recall",
        "Чтобы подтвердить адрес электронной почты, откройте ссылку в течение 24 часов:\n\n"
        + link
        + "\n\nЕсли вы не запрашивали это письмо, просто проигнорируйте его.",
    )
    return {"status": "ok", "message": "Письмо для подтверждения отправлено."}


@app.post("/auth/email/verify")
def verify_email(
    payload: EmailRequestToken,
    conn: Annotated[Connection, Depends(get_db)],
) -> dict[str, str]:
    token_hash = hashlib.sha256(payload.token.encode("ascii", errors="ignore")).hexdigest()
    row = conn.execute(
        "SELECT public.recall_consume_email_verification(%s) AS consumed",
        (token_hash,),
    ).fetchone()
    if not row or not row["consumed"]:
        raise HTTPException(status_code=400, detail="Ссылка недействительна, уже использована или устарела.")
    return {"status": "ok", "message": "Email подтверждён."}


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
