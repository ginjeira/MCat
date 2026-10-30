"""Per-website-user Telegram authorization, encrypted session storage, and destination selection."""
import asyncio
import os
import re
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient, types
from telethon.errors import (
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from .web_service import TelegramWebService, utc_text


class UserTelegramManager:
    """Owns one encrypted Telegram session and default destination per web user."""

    def __init__(self, db_path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.api_id = int(os.getenv("TG_API_ID", "0") or 0)
        self.api_hash = os.getenv("TG_API_HASH", "").strip()
        self.bot_name = os.getenv("TG_BOT_USERNAME", "").strip()
        key = os.getenv("MCAT_WEB_TG_ENCRYPTION_KEY", "").strip()
        if not key:
            raise RuntimeError("Configura MCAT_WEB_TG_ENCRYPTION_KEY para proteger as sessões Telegram.")
        try:
            self.cipher = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("MCAT_WEB_TG_ENCRYPTION_KEY não é uma chave Fernet válida.") from exc
        if not all((self.api_id, self.api_hash, self.bot_name)):
            raise RuntimeError("Configuração Telegram incompleta.")
        self.db = sqlite3.connect(self.db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS web_telegram_accounts (
          username TEXT PRIMARY KEY, telegram_user_id INTEGER NOT NULL,
          phone_hint TEXT NOT NULL, session_ciphertext BLOB NOT NULL,
          destination_id INTEGER, destination_title TEXT,
          connected_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS web_jobs (
          id TEXT PRIMARY KEY, username TEXT NOT NULL, search_id TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          error TEXT);
        CREATE TABLE IF NOT EXISTS web_job_items (
          job_id TEXT NOT NULL, option_number INTEGER NOT NULL, title TEXT NOT NULL,
          status TEXT NOT NULL, result_json TEXT, error TEXT,
          PRIMARY KEY(job_id, option_number));
        """)
        self.db.commit()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        self.pending = {}
        self.services = {}
        self.worker_tasks = {}
        self.code_cooldowns = {}

    async def connect(self):
        rows = self.db.execute("SELECT * FROM web_telegram_accounts").fetchall()
        for row in rows:
            try:
                session_string = self.cipher.decrypt(row["session_ciphertext"]).decode("ascii")
            except (InvalidToken, UnicodeError) as exc:
                raise RuntimeError("Não foi possível desbloquear uma sessão Telegram guardada.") from exc
            service = await self._make_service(session_string, row["username"])
            if row["destination_id"] is not None:
                dialogs = await self._eligible_dialogs(service)
                match = next((entity for entity, _ in dialogs
                              if entity.id == row["destination_id"]), None)
                if match:
                    service.set_destination(match)
            self.services[row["username"]] = service
            self.worker_tasks[row["username"]] = asyncio.create_task(service.worker())

    async def close(self):
        for task in list(self.worker_tasks.values()):
            task.cancel()
        for task in list(self.worker_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.worker_tasks.clear()
        for service in list(self.services.values()):
            await service.close()
        self.services.clear()
        for state in list(self.pending.values()):
            try:
                await state["client"].disconnect()
            except Exception:
                pass
        self.pending.clear()
        self.db.close()

    async def _make_service(self, session_string, username):
        service = TelegramWebService(session_string=session_string, per_user=True, web_username=username)
        await service.connect()
        return service

    def _service(self, username):
        service = self.services.get(username)
        if not service:
            raise RuntimeError("Liga primeiro uma conta Telegram nas definições da conta.")
        if not service.ready:
            raise RuntimeError(service.startup_error or "A ligação Telegram desta conta não está disponível.")
        return service

    def get_telegram_state(self, username):
        row = self.db.execute(
            "SELECT telegram_user_id,phone_hint,destination_id,destination_title FROM web_telegram_accounts WHERE username=?",
            (username,),
        ).fetchone()
        if not row:
            return {"supported": True, "connected": False, "destination": None}
        service = self.services.get(username)
        destination = None
        if row["destination_id"] is not None:
            destination = {"id": row["destination_id"], "title": row["destination_title"],
                           "available": bool(service and service.destination)}
        return {"supported": True, "connected": bool(service and service.ready),
                "telegram_user_id": row["telegram_user_id"], "phone_hint": row["phone_hint"],
                "destination": destination,
                "ready": bool(service and service.ready)}

    async def send_code(self, username, phone):
        phone = str(phone).strip()
        if not re.fullmatch(r"\+?[1-9]\d{6,14}", phone):
            raise ValueError("Indica um número Telegram válido com indicativo do país.")
        if username in self.services:
            raise ValueError("Esta conta Telegram já está ligada. Desliga-a antes de ligar outra.")
        now = time.monotonic()
        if self.code_cooldowns.get(username, 0) > now:
            raise ValueError("Aguarda um minuto antes de pedir outro código.")
        previous = self.pending.pop(username, None)
        if previous:
            await previous["client"].disconnect()
        client = TelegramClient(StringSession(), self.api_id, self.api_hash)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as exc:
            await client.disconnect()
            raise RuntimeError("O Telegram não conseguiu enviar um código para esse número.") from exc
        hint = "••••" + re.sub(r"\D", "", phone)[-4:]
        self.pending[username] = {
            "client": client, "phone": phone, "phone_code_hash": sent.phone_code_hash,
            "phone_hint": hint, "expires": now + 600, "password_required": False, "attempts": 0,
        }
        self.code_cooldowns[username] = now + 60
        return {"code_sent": True, "phone_hint": hint}

    async def verify_code(self, username, code):
        state = self._pending_state(username)
        try:
            await state["client"].sign_in(
                phone=state["phone"], code=str(code).strip(),
                phone_code_hash=state["phone_code_hash"])
        except SessionPasswordNeededError:
            state["password_required"] = True
            return {"authenticated": False, "password_required": True}
        except PhoneCodeInvalidError as exc:
            state["attempts"] += 1
            if state["attempts"] >= 5:
                await self._discard_pending(username)
                raise ValueError("Demasiadas tentativas. Pede um novo código Telegram.") from exc
            raise ValueError("O código Telegram está incorreto.") from exc
        except PhoneCodeExpiredError as exc:
            await self._discard_pending(username)
            raise ValueError("O código Telegram expirou. Pede um código novo.") from exc
        except Exception as exc:
            raise RuntimeError("O Telegram não confirmou o código de início de sessão.") from exc
        await self._finish_link(username, state)
        return {"authenticated": True, "password_required": False}

    async def verify_password(self, username, password):
        state = self._pending_state(username)
        if not state["password_required"]:
            raise ValueError("O Telegram ainda não pediu a palavra-passe de verificação em dois passos.")
        try:
            await state["client"].sign_in(password=str(password))
        except PasswordHashInvalidError as exc:
            state["attempts"] += 1
            if state["attempts"] >= 5:
                await self._discard_pending(username)
                raise ValueError("Demasiadas tentativas. Pede um novo código Telegram.") from exc
            raise ValueError("A palavra-passe de verificação Telegram está incorreta.") from exc
        except Exception as exc:
            raise RuntimeError("O Telegram não confirmou a palavra-passe de verificação.") from exc
        await self._finish_link(username, state)
        return {"authenticated": True}

    def _pending_state(self, username):
        state = self.pending.get(username)
        if not state:
            raise ValueError("Não há uma ligação Telegram pendente. Pede um novo código.")
        if state["expires"] < time.monotonic():
            raise ValueError("A ligação expirou. Pede um novo código.")
        return state

    async def _discard_pending(self, username):
        state = self.pending.pop(username, None)
        if state:
            await state["client"].disconnect()

    async def _finish_link(self, username, state):
        client = state["client"]
        me = await client.get_me()
        if not me:
            await self._discard_pending(username)
            raise RuntimeError("O Telegram não confirmou a conta.")
        session_string = client.session.save()
        await client.disconnect()
        ciphertext = self.cipher.encrypt(session_string.encode("ascii"))
        service = await self._make_service(session_string, username)
        self.db.execute("""
          INSERT INTO web_telegram_accounts
            (username,telegram_user_id,phone_hint,session_ciphertext,connected_at)
          VALUES(?,?,?,?,?)
          ON CONFLICT(username) DO UPDATE SET
            telegram_user_id=excluded.telegram_user_id,
            phone_hint=excluded.phone_hint,
            session_ciphertext=excluded.session_ciphertext,
            destination_id=NULL,destination_title=NULL,
            connected_at=excluded.connected_at
        """, (username, me.id, state["phone_hint"], ciphertext, utc_text()))
        self.db.commit()
        old = self.services.pop(username, None)
        old_task = self.worker_tasks.pop(username, None)
        if old_task:
            old_task.cancel()
            try:
                await old_task
            except asyncio.CancelledError:
                pass
        if old:
            await old.close()
        self.services[username] = service
        self.worker_tasks[username] = asyncio.create_task(service.worker())
        self.pending.pop(username, None)

    async def _eligible_dialogs(self, service):
        dialogs = []
        async for dialog in service.client.iter_dialogs(limit=None):
            entity = dialog.entity
            if not isinstance(entity, (types.Chat, types.Channel)):
                continue
            if getattr(entity, "left", False) or getattr(entity, "deactivated", False):
                continue
            if isinstance(entity, types.Channel) and entity.broadcast:
                rights = getattr(entity, "admin_rights", None)
                if not (getattr(entity, "creator", False)
                        or getattr(rights, "post_messages", False)):
                    continue
            try:
                permissions = await service.client.get_permissions(entity, "me")
                if getattr(permissions, "send_messages", False):
                    continue
            except Exception:
                if isinstance(entity, types.Channel) and not getattr(entity, "creator", False):
                    continue
            dialogs.append((entity, dialog))
        return dialogs

    async def list_destinations(self, username):
        service = self._service(username)
        result = []
        for entity, dialog in await self._eligible_dialogs(service):
            kind = "Canal" if isinstance(entity, types.Channel) and entity.broadcast else "Grupo"
            suffix = getattr(entity, "username", None) or str(entity.id)
            result.append({"id": entity.id, "title": dialog.name, "kind": kind,
                           "detail": suffix})
        return result

    async def select_destination(self, username, chat_id):
        service = self._service(username)
        try:
            wanted = int(chat_id)
        except (ValueError, TypeError) as exc:
            raise ValueError("O grupo selecionado é inválido.") from exc
        match = next(((entity, dialog) for entity, dialog in
                      await self._eligible_dialogs(service) if entity.id == wanted), None)
        if not match:
            raise ValueError("Esse grupo não está acessível ou não permite encaminhar mensagens.")
        entity, dialog = match
        self.db.execute(
            "UPDATE web_telegram_accounts SET destination_id=?,destination_title=? WHERE username=?",
            (entity.id, dialog.name, username))
        self.db.commit()
        service.set_destination(entity)
        return {"id": entity.id, "title": dialog.name, "selected": True}

    async def disconnect_account(self, username):
        if username in self.pending:
            await self._discard_pending(username)
        active = self.db.execute(
            "SELECT COUNT(*) FROM web_jobs WHERE username=? AND status IN ('queued','running')",
            (username,),
        ).fetchone()[0]
        if active:
            raise ValueError("Não é possível desligar a conta enquanto há pedidos em curso.")
        task = self.worker_tasks.pop(username, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        service = self.services.pop(username, None)
        if service:
            try:
                await service.client.log_out()
            except Exception as exc:
                self.services[username] = service
                self.worker_tasks[username] = asyncio.create_task(service.worker())
                raise RuntimeError("O Telegram não confirmou a revogação da sessão. A conta continua ligada.") from exc
            await service.close()
        self.db.execute("DELETE FROM web_telegram_accounts WHERE username=?", (username,))
        self.db.commit()
        return {"disconnected": True}

    async def search(self, username, query, content_type, provider):
        return await self._service(username).search(username, query, content_type, provider)

    async def create_job(self, username, search_id, selected_numbers):
        service = self._service(username)
        if service.destination is None:
            raise RuntimeError("Escolhe um grupo destino nas definições da conta.")
        eligible = await self._eligible_dialogs(service)
        if not any(entity.id == service.destination.id for entity, _ in eligible):
            service.set_destination(None)
            self.db.execute("UPDATE web_telegram_accounts SET destination_id=NULL,destination_title=NULL WHERE username=?",
                            (username,))
            self.db.commit()
            raise RuntimeError("O grupo destino deixou de estar acessível. Escolhe outro destino.")
        return service.create_job(username, search_id, selected_numbers)

    def get_jobs(self, username, job_id=None):
        service = self.services.get(username)
        if service:
            return service.get_jobs(username, job_id)
        if job_id:
            return None
        ids = self.db.execute(
            "SELECT id FROM web_jobs WHERE username=? ORDER BY created_at DESC LIMIT 20",
            (username,),
        ).fetchall()
        return [self._get_job_from_db(username, row["id"]) for row in ids]

    def _get_job_from_db(self, username, job_id):
        job = self.db.execute("SELECT * FROM web_jobs WHERE id=? AND username=?",
                              (job_id, username)).fetchone()
        if not job:
            return None
        rows = self.db.execute(
            "SELECT option_number,title,status,result_json,error FROM web_job_items WHERE job_id=? ORDER BY option_number",
            (job_id,),
        ).fetchall()
        import json
        return {"id": job["id"], "status": job["status"], "created_at": job["created_at"],
                "updated_at": job["updated_at"], "error": job["error"],
                "items": [{"number": row["option_number"], "title": row["title"],
                           "status": row["status"],
                           "result": json.loads(row["result_json"]) if row["result_json"] else None,
                           "error": row["error"]} for row in rows]}
