"""HTTP interface for authenticated MCat search and forwarding requests."""
import asyncio
import hashlib
import hmac
import json
import os
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

SESSION_COOKIE = "mcat_session"
SESSION_TTL_SECONDS = 8 * 60 * 60
DB_PATH = Path(os.getenv("DB_PATH", "./data/mcat.sqlite3")).expanduser()
WEB_USERNAME = os.getenv("MCAT_WEB_USERNAME", "").strip()
WEB_PASSWORD = os.getenv("MCAT_WEB_PASSWORD", "")
COOKIE_SECURE = os.getenv("MCAT_WEB_COOKIE_SECURE", "1").strip().casefold() not in ("0", "false", "no")


def _init_auth_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS web_sessions (
        token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
        csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions(expires_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS web_login_attempts (
        client_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL,
        locked_until TEXT NOT NULL)""")
    db.commit()
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    return db


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc)


@asynccontextmanager
async def lifespan(app):
    app.state.auth_db = _init_auth_database()
    app.state.telegram = TelegramWebService()
    await app.state.telegram.connect()
    app.state.worker_task = asyncio.create_task(app.state.telegram.worker())
    try:
        yield
    finally:
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
    row = db.execute("SELECT * FROM web_sessions WHERE token_hash=?", (token_digest,)).fetchone()
    if not row:
        return None
    expires = datetime.fromisoformat(row[3])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= _now():
        db.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_digest,))
        db.commit()
        return None
    return {"username": row[1], "csrf": row[2], "token_hash": token_digest}


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
    return {"authenticated": True, "username": session["username"], "csrf": session["csrf"]}


@app.post("/api/login")
async def login(request: Request, response: Response):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Pedido inválido.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Pedido inválido.")
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not WEB_USERNAME or not WEB_PASSWORD:
        raise HTTPException(status_code=503, detail="A autenticação web ainda não está configurada no servidor.")
    client_host = request.client.host if request.client else "unknown"
    client_hash = hashlib.sha256(client_host.encode("utf-8")).hexdigest()
    db = request.app.state.auth_db
    attempt = db.execute("SELECT failures,locked_until FROM web_login_attempts WHERE client_hash=?",
                         (client_hash,)).fetchone()
    if attempt and attempt[1] > utc_text():
        raise HTTPException(status_code=429, detail="Demasiadas tentativas. Tenta novamente mais tarde.")
    if not (hmac.compare_digest(username, WEB_USERNAME) and hmac.compare_digest(password, WEB_PASSWORD)):
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
    response = JSONResponse({"authenticated": True, "username": username, "csrf": csrf})
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS, httponly=True,
                        secure=COOKIE_SECURE, samesite="strict", path="/")
    return response


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
    uvicorn.run("mcat.web:app", host=host, port=port, proxy_headers=False)


if __name__ == "__main__":
    run()

