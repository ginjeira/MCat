"""HTTP interface for authenticated MCat search and forwarding requests."""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from .web_service import TelegramWebService, utc_text

load_dotenv()

logger = logging.getLogger(__name__)

SESSION_COOKIE = os.getenv("MCAT_WEB_SESSION_COOKIE", "mcat_session").strip()
SESSION_TTL_SECONDS = 8 * 60 * 60
DB_PATH = Path(os.getenv("DB_PATH", "./data/mcat.sqlite3")).expanduser()
AUTH_DB_PATH = Path(os.getenv("MCAT_WEB_AUTH_DB_PATH", str(DB_PATH))).expanduser()
WEB_USERNAME = os.getenv("MCAT_WEB_USERNAME", "").strip()
WEB_PASSWORD = os.getenv("MCAT_WEB_PASSWORD", "")
COOKIE_SECURE = os.getenv("MCAT_WEB_COOKIE_SECURE", "1").strip().casefold() not in ("0", "false", "no")


def _init_auth_database():
    AUTH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(AUTH_DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS web_users (
        username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','user')),
        is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS web_sessions (
        token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
        csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions(expires_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS web_login_attempts (
        client_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL,
        locked_until TEXT NOT NULL)""")
    user_count = db.execute("SELECT COUNT(*) AS n FROM web_users").fetchone()["n"]
    if user_count == 0:
        if not WEB_USERNAME or not WEB_PASSWORD:
            raise RuntimeError("Configura MCAT_WEB_USERNAME e MCAT_WEB_PASSWORD para criar o primeiro administrador.")
        db.execute(
            "INSERT INTO web_users(username,password_hash,role,is_active,created_at) VALUES(?,?,'admin',1,?)",
            (WEB_USERNAME.casefold(), _hash_password(WEB_PASSWORD), utc_text()),
        )
    db.commit()
    try:
        os.chmod(AUTH_DB_PATH, 0o600)
    except OSError:
        pass
    return db


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_password(password):
    iterations = 310_000
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii")
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt_text, digest_text)


def _verify_password(password, encoded):
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def _now():
    return datetime.now(timezone.utc)


@asynccontextmanager
async def lifespan(app):
    app.state.auth_db = _init_auth_database()
    if os.getenv("MCAT_WEB_TG_MODE", "shared").strip().casefold() == "per_user":
        from .telegram_accounts import UserTelegramManager
        app.state.telegram = UserTelegramManager(DB_PATH)
        await app.state.telegram.connect()
        app.state.worker_task = None
    else:
        app.state.telegram = TelegramWebService()
        await app.state.telegram.connect()
        app.state.worker_task = asyncio.create_task(app.state.telegram.worker())
    try:
        yield
    finally:
        if app.state.worker_task:
            app.state.worker_task.cancel()
            try:
                await app.state.worker_task
            except asyncio.CancelledError:
                pass
        await app.state.telegram.close()
        app.state.auth_db.close()


app = FastAPI(title="MCat", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


async def _session(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    db = request.app.state.auth_db
    token_digest = _token_hash(token)
    row = db.execute(
        """SELECT s.username,s.csrf_token,s.expires_at,u.role,u.is_active
           FROM web_sessions s LEFT JOIN web_users u ON u.username=s.username
           WHERE s.token_hash=?""",
        (token_digest,),
    ).fetchone()
    if not row:
        return None
    if not row["is_active"]:
        db.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_digest,))
        db.commit()
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= _now():
        db.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_digest,))
        db.commit()
        return None
    return {"username": row["username"], "role": row["role"],
            "csrf": row["csrf_token"], "token_hash": token_digest}


async def _require_session(request: Request, csrf=False):
    session = await _session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Inicia sessão para continuar.")
    if csrf:
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied or not hmac.compare_digest(supplied, session["csrf"]):
            raise HTTPException(status_code=403, detail="Token de segurança inválido; atualiza a página.")
    return session


@app.get("/", response_class=HTMLResponse)
async def index():
    content = Path(__file__).with_name("web.html").read_text(encoding="utf-8")
    return HTMLResponse(content, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
    })


@app.get("/api/me")
async def who_am_i(request: Request):
    session = await _session(request)
    if not session:
        return {"authenticated": False}
    return {"authenticated": True, "username": session["username"],
            "role": session["role"], "csrf": session["csrf"]}


def _telegram_manager(request):
    manager = request.app.state.telegram
    if not hasattr(manager, "get_telegram_state"):
        raise HTTPException(status_code=404, detail="A ligação individual Telegram não está ativa nesta versão.")
    return manager


async def _telegram_request_body(request):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Pedido inválido.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Pedido inválido.")
    return body


@app.get("/api/telegram/status")
async def telegram_status(request: Request):
    session = await _require_session(request)
    manager = request.app.state.telegram
    if not hasattr(manager, "get_telegram_state"):
        return {"supported": False, "connected": False, "destination": None}
    return manager.get_telegram_state(session["username"])


@app.post("/api/telegram/send-code")
async def telegram_send_code(request: Request):
    session = await _require_session(request, csrf=True)
    body = await _telegram_request_body(request)
    try:
        return await _telegram_manager(request).send_code(session["username"], body.get("phone", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/telegram/verify-code")
async def telegram_verify_code(request: Request):
    session = await _require_session(request, csrf=True)
    body = await _telegram_request_body(request)
    try:
        return await _telegram_manager(request).verify_code(session["username"], body.get("code", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/telegram/verify-password")
async def telegram_verify_password(request: Request):
    session = await _require_session(request, csrf=True)
    body = await _telegram_request_body(request)
    try:
        return await _telegram_manager(request).verify_password(session["username"], body.get("password", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/telegram/destinations")
async def telegram_destinations(request: Request):
    session = await _require_session(request)
    try:
        destinations = await _telegram_manager(request).list_destinations(session["username"])
        return {"destinations": destinations}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/telegram/destination")
async def telegram_select_destination(request: Request):
    session = await _require_session(request, csrf=True)
    body = await _telegram_request_body(request)
    try:
        return await _telegram_manager(request).select_destination(session["username"], body.get("chat_id"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/telegram/disconnect")
async def telegram_disconnect(request: Request):
    session = await _require_session(request, csrf=True)
    try:
        return await _telegram_manager(request).disconnect_account(session["username"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/login")
async def login(request: Request, response: Response):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Pedido inválido.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Pedido inválido.")
    username = str(body.get("username", "")).strip().casefold()
    password = str(body.get("password", ""))
    client_host = request.client.host if request.client else "unknown"
    client_hash = hashlib.sha256(client_host.encode("utf-8")).hexdigest()
    db = request.app.state.auth_db
    attempt = db.execute("SELECT failures,locked_until FROM web_login_attempts WHERE client_hash=?",
                         (client_hash,)).fetchone()
    if attempt and attempt[1] > utc_text():
        raise HTTPException(status_code=429, detail="Demasiadas tentativas. Tenta novamente mais tarde.")
    user = db.execute("SELECT password_hash,is_active,role FROM web_users WHERE username=?", (username,)).fetchone()
    valid_password = _verify_password(password, user["password_hash"]) if user else False
    if not user or not user["is_active"] or not valid_password:
        failures = (attempt[0] if attempt else 0) + 1
        locked_until = utc_text(datetime.now(timezone.utc) + timedelta(minutes=15 if failures >= 8 else 0))
        if failures >= 8:
            db.execute("INSERT INTO web_login_attempts(client_hash,failures,locked_until) VALUES(?,?,?) ON CONFLICT(client_hash) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until",
                       (client_hash, failures, locked_until))
        else:
            db.execute("INSERT INTO web_login_attempts(client_hash,failures,locked_until) VALUES(?,?,?) ON CONFLICT(client_hash) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until",
                       (client_hash, failures, utc_text()))
        db.commit()
        raise HTTPException(status_code=401, detail="Utilizador ou palavra-passe incorretos.")

    db.execute("DELETE FROM web_login_attempts WHERE client_hash=?", (client_hash,))
    db.execute("DELETE FROM web_sessions WHERE expires_at<=?", (utc_text(),))
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    expires = _now() + timedelta(seconds=SESSION_TTL_SECONDS)
    db.execute("INSERT INTO web_sessions(token_hash,username,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)",
               (_token_hash(token), username, csrf, utc_text(expires), utc_text()))
    db.commit()
    response = JSONResponse({"authenticated": True, "username": username,
                             "role": user["role"], "csrf": csrf})
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS, httponly=True,
                        secure=COOKIE_SECURE, samesite="strict", path="/")
    return response


@app.post("/api/password")
async def change_password(request: Request):
    session = await _require_session(request, csrf=True)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Pedido inválido.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Pedido inválido.")
    current = str(body.get("current_password", ""))
    new_password = str(body.get("new_password", ""))
    if len(new_password) < 14:
        raise HTTPException(status_code=400, detail="A nova palavra-passe deve ter pelo menos 14 caracteres.")
    db = request.app.state.auth_db
    row = db.execute("SELECT password_hash FROM web_users WHERE username=? AND is_active=1",
                     (session["username"],)).fetchone()
    if not row or not _verify_password(current, row["password_hash"]):
        raise HTTPException(status_code=400, detail="A palavra-passe atual está incorreta.")
    if hmac.compare_digest(current, new_password):
        raise HTTPException(status_code=400, detail="A nova palavra-passe tem de ser diferente.")
    db.execute("UPDATE web_users SET password_hash=? WHERE username=?",
               (_hash_password(new_password), session["username"]))
    db.execute("DELETE FROM web_sessions WHERE username=? AND token_hash<>?",
               (session["username"], session["token_hash"]))
    db.commit()
    return {"changed": True}


@app.get("/api/admin/users")
async def list_users(request: Request):
    session = await _require_session(request)
    if session["role"] != "admin":
        raise HTTPException(status_code=403, detail="Só o administrador pode gerir utilizadores.")
    rows = request.app.state.auth_db.execute(
        "SELECT username,role,is_active,created_at FROM web_users ORDER BY username"
    ).fetchall()
    return {"users": [dict(row) for row in rows]}


@app.post("/api/admin/users")
async def create_user(request: Request):
    session = await _require_session(request, csrf=True)
    if session["role"] != "admin":
        raise HTTPException(status_code=403, detail="Só o administrador pode criar utilizadores.")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Pedido inválido.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Pedido inválido.")
    username = str(body.get("username", "")).strip().casefold()
    password = str(body.get("password", ""))
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", username):
        raise HTTPException(status_code=400, detail="O utilizador deve ter 3 a 32 caracteres: letras, números, ponto, hífen ou underscore.")
    if len(password) < 14:
        raise HTTPException(status_code=400, detail="A palavra-passe inicial deve ter pelo menos 14 caracteres.")
    db = request.app.state.auth_db
    try:
        db.execute("INSERT INTO web_users(username,password_hash,role,is_active,created_at) VALUES(?,?,'user',1,?)",
                   (username, _hash_password(password), utc_text()))
        db.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Esse utilizador já existe.") from exc
    return {"username": username, "role": "user", "created": True}


@app.post("/api/admin/users/{username}/password")
async def set_user_password(username: str, request: Request):
    session = await _require_session(request, csrf=True)
    if session["role"] != "admin":
        raise HTTPException(status_code=403, detail="Só o administrador pode gerir utilizadores.")
    password = str((await request.json()).get("password", ""))
    if len(password) < 14:
        raise HTTPException(status_code=400, detail="A palavra-passe deve ter pelo menos 14 caracteres.")
    db = request.app.state.auth_db
    target = username.strip().casefold()
    cursor = db.execute("UPDATE web_users SET password_hash=? WHERE username=?",
                        (_hash_password(password), target))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Utilizador não encontrado.")
    db.execute("DELETE FROM web_sessions WHERE username=?", (target,))
    db.commit()
    return {"username": target, "updated": True}


@app.post("/api/logout")
async def logout(request: Request):
    session = await _require_session(request, csrf=True)
    request.app.state.auth_db.execute("DELETE FROM web_sessions WHERE token_hash=?", (session["token_hash"],))
    request.app.state.auth_db.commit()
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="strict")
    return response


@app.post("/api/search")
async def search(request: Request):
    session = await _require_session(request, csrf=True)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Pedido inválido.")
        query = str(body.get("query", "")).strip()
        content_type = str(body.get("content_type", ""))
        provider = str(body.get("provider", ""))
        if not 2 <= len(query) <= 150:
            raise ValueError("O termo de pesquisa deve ter entre 2 e 150 caracteres.")
        return await request.app.state.telegram.search(session["username"], query, content_type, provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Web search failed for user %s", session["username"])
        raise HTTPException(status_code=502, detail=f"Não foi possível concluir a pesquisa ({type(exc).__name__}).") from exc


@app.post("/api/jobs")
async def create_job(request: Request):
    session = await _require_session(request, csrf=True)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("A seleção de opções é inválida.")
        search_id = str(body.get("search_id", ""))
        values = body.get("selected_numbers", [])
        if not isinstance(values, list) or any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise ValueError("A seleção de opções é inválida.")
        job_id = request.app.state.telegram.create_job(session["username"], search_id, values)
        if asyncio.iscoroutine(job_id):
            job_id = await job_id
        return JSONResponse({"job_id": job_id, "status": "queued"}, status_code=202)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/jobs")
async def jobs(request: Request):
    session = await _require_session(request)
    return {"jobs": request.app.state.telegram.get_jobs(session["username"])}


@app.get("/api/jobs/{job_id}")
async def job(request: Request, job_id: str):
    session = await _require_session(request)
    result = request.app.state.telegram.get_jobs(session["username"], job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
    return result


def run():
    import uvicorn
    host = os.getenv("MCAT_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("MCAT_WEB_PORT", "8080"))
    certfile = os.getenv("MCAT_WEB_SSL_CERTFILE", "").strip() or None
    keyfile = os.getenv("MCAT_WEB_SSL_KEYFILE", "").strip() or None
    if bool(certfile) != bool(keyfile):
        raise SystemExit("Configura MCAT_WEB_SSL_CERTFILE e MCAT_WEB_SSL_KEYFILE em conjunto.")
    try:
        loopback_only = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback_only = host.casefold() == "localhost"
    allow_http_origin = os.getenv("MCAT_WEB_ALLOW_HTTP_ORIGIN", "0").strip().casefold() in ("1", "true", "yes")
    if not loopback_only and not (certfile and keyfile) and not allow_http_origin:
        raise SystemExit("O acesso fora do próprio servidor exige HTTPS. Para uma origem atrás de túnel privado, define explicitamente MCAT_WEB_ALLOW_HTTP_ORIGIN=1.")
    uvicorn.run("mcat.web:app", host=host, port=port, proxy_headers=False,
                ssl_certfile=certfile, ssl_keyfile=keyfile)


if __name__ == "__main__":
    run()

