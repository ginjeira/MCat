"""Authenticated web workflow backed by the configured Telegram user session."""
import asyncio
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.types import InputMessagesFilterMusic

from .playlist_engine import (
    MAINTENANCE_COOLDOWN_SECONDS,
    add_media,
    audio_metadata,
    finish_playlist,
    index_destination_media,
    init_playlist_schema,
    is_finished,
    is_maintenance_notice,
    is_playlist_start,
    maintenance_cooldown_remaining,
    media_document_id,
    set_maintenance_cooldown,
    start_playlist,
)

load_dotenv()


def utc_now():
    return datetime.now(timezone.utc)


def utc_text(value=None):
    return (value or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def normalize(value):
    value = unicodedata.normalize("NFKD", (value or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


class TelegramWebService:
    """Serializes bot interactions and keeps all Telegram credentials server-side."""

    CONTENT_TYPES = {
        "track": ("Tracks", "track"),
        "album": ("Albums", "album"),
        "playlist": ("Playlists", "playlist"),
        "artist": ("Artists", "artist"),
    }
    PROVIDERS = {"deezer": "Deezer", "soundcloud": "Soundcloud"}

    def __init__(self, session_string=None, per_user=False):
        self.session_string = session_string
        self.per_user = per_user
        self.api_id = int(os.getenv("TG_API_ID", "0") or 0)
        self.api_hash = os.getenv("TG_API_HASH", "").strip()
        self.bot_name = os.getenv("TG_BOT_USERNAME", "").strip().lstrip("@")
        self.destination_ref = "" if per_user else os.getenv("TG_DESTINATION", "").strip()
        self.source_refs = [] if per_user else [item.strip() for item in os.getenv("TG_DEDUP_SOURCES", "").split(",") if item.strip()]
        self.session_name = os.getenv("MCAT_WEB_TG_SESSION", "mcat_user_session")
        self.db_path = Path(os.getenv("DB_PATH", "./data/mcat.sqlite3")).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        init_playlist_schema(self.db)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS web_searches (
          id TEXT PRIMARY KEY, username TEXT NOT NULL, query TEXT NOT NULL,
          content_type TEXT NOT NULL, provider TEXT NOT NULL,
          bot_message_id INTEGER NOT NULL, results_json TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS web_jobs (
          id TEXT PRIMARY KEY, username TEXT NOT NULL, search_id TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          error TEXT);
        CREATE TABLE IF NOT EXISTS web_job_items (
          job_id TEXT NOT NULL, option_number INTEGER NOT NULL, title TEXT NOT NULL,
          status TEXT NOT NULL, result_json TEXT, error TEXT,
          PRIMARY KEY(job_id, option_number));
        """)
        self.db.execute("UPDATE web_jobs SET status='interrupted',updated_at=?,error=? WHERE status IN ('queued','running')",
                        (utc_text(), "O serviço reiniciou antes de concluir o pedido; não foi retomado automaticamente."))
        self.db.commit()
        self.client = None
        self.bot = None
        self.destination = None
        self.dedup_chats = []
        self.ready = False
        self.startup_error = None
        self.destination_indexed = False
        self.telegram_lock = asyncio.Lock()
        self.job_queue = asyncio.Queue()

    async def connect(self):
        if not all((self.api_id, self.api_hash, self.bot_name)):
            self.startup_error = "Configuração Telegram incompleta."
            return
        telegram_session = StringSession(self.session_string) if self.session_string is not None else self.session_name
        self.client = TelegramClient(telegram_session, self.api_id, self.api_hash)
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                self.startup_error = "A sessão Telegram do servidor não está autenticada."
                return
            self.bot = await self.client.get_entity(self.bot_name)
            if self.destination_ref:
                try:
                    self.destination = await self.client.get_entity(self.destination_ref)
                except Exception:
                    self.destination = None
                    # Telethon expects a cached channel entity, not the Bot API
                    # style "-100..." channel id. Resolve configured numeric IDs
                    # against the account's dialogs, which also verifies access.
                    ref = self.destination_ref
                    if re.fullmatch(r"-?\d+", ref):
                        target_id = int(ref[4:] if ref.startswith("-100") else ref.lstrip("-"))
                        async for dialog in self.client.iter_dialogs():
                            if getattr(dialog.entity, "id", None) == target_id:
                                self.destination = dialog.entity
                                break
            self.dedup_chats = []
            if self.destination is not None:
                self.dedup_chats.append(self.destination)
            for reference in self.source_refs:
                try:
                    entity = await self.client.get_entity(reference)
                    if all(getattr(existing, "id", None) != getattr(entity, "id", None) for existing in self.dedup_chats):
                        self.dedup_chats.append(entity)
                except Exception:
                    continue
            self.ready = self.bot is not None
            if not self.ready:
                self.startup_error = "O bot Telegram não pôde ser resolvido."
        except Exception as exc:
            self.startup_error = f"Não foi possível iniciar a ligação Telegram ({type(exc).__name__})."

    def set_destination(self, entity):
        self.destination = entity
        self.dedup_chats = [entity]
        self.destination_indexed = False

    async def close(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        self.db.close()

    def check_available(self, require_destination=False):
        if not self.ready:
            raise RuntimeError(self.startup_error or "A ligação Telegram não está disponível.")
        if require_destination and self.destination is None:
            raise RuntimeError("O grupo destino não está configurado ou acessível.")

    async def _latest_bot_message_id(self):
        messages = await self.client.get_messages(self.bot, limit=1)
        return messages[0].id if messages else 0

    @staticmethod
    def _button_label(button):
        return (getattr(button, "text", "") or "").strip()

    @staticmethod
    def _button_selected(label):
        return label.startswith(("✅", "✔️", "✔"))

    @staticmethod
    def _callback_data(button):
        raw = getattr(button, "button", button)
        button_type = getattr(raw, "type", None)
        return getattr(button_type, "data", None)

    async def _click(self, message, row_index, column_index):
        button = message.buttons[row_index][column_index]
        data = self._callback_data(button)
        if isinstance(data, bytes):
            return await self.client(functions.messages.GetBotCallbackAnswerRequest(
                peer=message.input_chat, msg_id=message.id, data=data))
        return await button.click()

    async def _wait_message(self, message_id, predicate, timeout=20):
        deadline = asyncio.get_running_loop().time() + timeout
        last = None
        while asyncio.get_running_loop().time() < deadline:
            last = await self.client.get_messages(self.bot, ids=message_id)
            if last and predicate(last):
                return last
            await asyncio.sleep(0.3)
        return last if last and predicate(last) else None

    def _find_filter_button(self, message, label):
        wanted = normalize(label)
        for row_index, row in enumerate(getattr(message, "buttons", None) or []):
            for column_index, button in enumerate(row):
                actual = normalize(self._button_label(button))
                if actual.endswith(wanted):
                    return row_index, column_index, self._button_label(button)
        return None

    async def _set_filter(self, message, label, selected, state_predicate):
        found = self._find_filter_button(message, label)
        if not found:
            raise RuntimeError(f"O bot não apresentou o filtro {label}.")
        row_index, column_index, current_label = found
        if self._button_selected(current_label) == selected:
            return message
        try:
            await self._click(message, row_index, column_index)
        except Exception as exc:
            # A callback timeout is ambiguous. Observe the same message before
            # retrying; only retry once if Telegram still shows the old state.
            refreshed = await self._wait_message(message.id, state_predicate, timeout=8)
            if refreshed:
                return refreshed
            unchanged = await self.client.get_messages(self.bot, ids=message.id)
            if not unchanged or self._filter_active(unchanged, label) != self._button_selected(current_label):
                raise RuntimeError(
                    f"O resultado do clique em {label} ficou ambíguo; não foi repetido."
                ) from exc
            retry = self._find_filter_button(unchanged, label)
            if not retry:
                raise RuntimeError(f"O bot removeu o filtro {label} antes da repetição.") from exc
            try:
                await self._click(unchanged, retry[0], retry[1])
            except Exception as retry_exc:
                refreshed = await self._wait_message(message.id, state_predicate, timeout=8)
                if refreshed:
                    return refreshed
                raise RuntimeError(
                    f"O Telegram não confirmou a alteração de {label} após uma repetição controlada "
                    f"({type(retry_exc).__name__})."
                ) from retry_exc
            refreshed = await self._wait_message(message.id, state_predicate, timeout=8)
            if not refreshed:
                raise RuntimeError(f"O filtro {label} não foi confirmado após uma repetição controlada.")
            return refreshed
        refreshed = await self._wait_message(message.id, state_predicate, timeout=8)
        if not refreshed:
            raise RuntimeError(f"O filtro {label} não foi confirmado pelo Telegram.")
        return refreshed

    @staticmethod
    def _filter_active(message, label):
        wanted = normalize(label)
        for row in getattr(message, "buttons", None) or []:
            for button in row:
                actual = normalize(TelegramWebService._button_label(button))
                if actual.endswith(wanted):
                    return TelegramWebService._button_selected(TelegramWebService._button_label(button))
        return False

    async def search(self, username, query, content_type, provider):
        self.check_available()
        remaining = maintenance_cooldown_remaining(self.db)
        if remaining:
            raise RuntimeError(f"Pesquisas suspensas por manutenção; tenta novamente dentro de cerca de {(remaining + 59) // 60} min.")
        if content_type not in self.CONTENT_TYPES or provider not in self.PROVIDERS:
            raise ValueError("Tipo de conteúdo ou fornecedor inválido.")
        async with self.telegram_lock:
            minimum_id = await self._latest_bot_message_id()
            sent = await self.client.send_message(self.bot, query)
            deadline = asyncio.get_running_loop().time() + 35
            result_message = None
            while asyncio.get_running_loop().time() < deadline:
                recent = await self.client.get_messages(self.bot, limit=10)
                result_message = next((item for item in recent
                                       if item.id > max(minimum_id, sent.id)
                                       and re.search(r"Searching\s+for", item.message or "", re.I)
                                       and getattr(item, "buttons", None)), None)
                maintenance = next((item for item in recent
                                    if item.id > max(minimum_id, sent.id)
                                    and is_maintenance_notice(item.message or "")), None)
                if maintenance:
                    set_maintenance_cooldown(self.db)
                    raise RuntimeError(
                        f"O bot está em manutenção; pesquisas suspensas por "
                        f"{MAINTENANCE_COOLDOWN_SECONDS // 60} minutos."
                    )
                rejected = next((item for item in recent
                                 if item.id > max(minimum_id, sent.id)
                                 and re.search(r"daily\s+search\s+limit\s+reached|please\s+search\s+in\s+your\s+music\s+app",
                                               item.message or "", re.I)), None)
                if rejected:
                    raise RuntimeError("O bot recusou a pesquisa por ter sido atingido o limite diário.")
                if result_message:
                    break
                await asyncio.sleep(0.3)
            if result_message is None:
                raise RuntimeError("O bot não devolveu resultados dentro do prazo.")

            type_label = self.CONTENT_TYPES[content_type][0]
            provider_label = self.PROVIDERS[provider]
            type_order = [(content_type, self.CONTENT_TYPES[content_type][0])]
            type_order.extend((key, value[0]) for key, value in self.CONTENT_TYPES.items() if key != content_type)
            for key, label in type_order:
                result_message = await self._set_filter(
                    result_message, label, key == content_type,
                    lambda item, name=label, wanted=(key == content_type):
                        self._filter_active(item, name) == wanted,
                )
            provider_order = [(provider, self.PROVIDERS[provider])]
            provider_order.extend((key, value) for key, value in self.PROVIDERS.items() if key != provider)
            for key, label in provider_order:
                result_message = await self._set_filter(
                    result_message, label, key == provider,
                    lambda item, name=label, wanted=(key == provider):
                        self._filter_active(item, name) == wanted,
                )

            def requested_mode(item):
                text = (item.message or "").casefold()
                type_pattern = {
                    "track": r"tracks?",
                    "album": r"albums?",
                    "playlist": r"playlists?",
                    "artist": r"artists?",
                }[content_type]
                type_match = re.search(r"in\s+['\"]?" + type_pattern + r"(?:['\"]|\b)", text)
                provider_match = re.search(r"from\s+['\"]?" + re.escape(provider) + r"(?:['\"]|\b)", text)
                return bool(type_match and provider_match)

            result_message = await self._wait_message(
                result_message.id, requested_mode, timeout=8)
            if not result_message:
                raise RuntimeError("O bot não confirmou o tipo e o fornecedor selecionados.")

            options = []
            for line in (result_message.message or "").splitlines():
                match = re.match(r"\s*(\d+)\.\s*(.+?)\s*$", line)
                if match:
                    options.append({"number": int(match.group(1)), "title": match.group(2)})
            available_numbers = {
                int(label) for row in (getattr(result_message, "buttons", None) or [])
                for button in row
                for label in [re.sub(r"\D", "", self._button_label(button))]
                if label.isdigit()
            }
            options = [item for item in options if item["number"] in available_numbers]
            if not options:
                raise RuntimeError("O bot não apresentou opções identificáveis para esta pesquisa.")

            search_id = uuid.uuid4().hex
            created = utc_now()
            self.db.execute(
                "INSERT INTO web_searches(id,username,query,content_type,provider,bot_message_id,results_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (search_id, username, query, content_type, provider, result_message.id,
                 json.dumps(options, ensure_ascii=False), utc_text(created),
                 utc_text(created + timedelta(minutes=20))),
            )
            self.db.commit()
            return {"id": search_id, "query": query, "content_type": content_type,
                    "provider": provider, "results": options, "expires_in_minutes": 20}

    def create_job(self, username, search_id, selected_numbers):
        self.check_available(require_destination=True)
        search = self.db.execute(
            "SELECT * FROM web_searches WHERE id=? AND username=? AND expires_at>?",
            (search_id, username, utc_text()),
        ).fetchone()
        if not search:
            raise ValueError("A pesquisa expirou ou não pertence a esta conta. Faz uma nova pesquisa.")
        prior_job = self.db.execute(
            "SELECT id FROM web_jobs WHERE search_id=? AND username=?",
            (search_id, username),
        ).fetchone()
        if prior_job:
            return prior_job["id"]
        options = json.loads(search["results_json"])
        by_number = {item["number"]: item["title"] for item in options}
        if not selected_numbers or len(selected_numbers) != len(set(selected_numbers)):
            raise ValueError("Seleciona uma ou mais opções diferentes.")
        if any(number not in by_number for number in selected_numbers):
            raise ValueError("Uma das opções selecionadas já não está disponível.")
        job_id = uuid.uuid4().hex
        stamp = utc_text()
        self.db.execute("INSERT INTO web_jobs(id,username,search_id,status,created_at,updated_at) VALUES(?,?,?,'queued',?,?)",
                        (job_id, username, search_id, stamp, stamp))
        for number in selected_numbers:
            self.db.execute("INSERT INTO web_job_items(job_id,option_number,title,status) VALUES(?,?,?,'queued')",
                            (job_id, number, by_number[number]))
        self.db.commit()
        self.job_queue.put_nowait(job_id)
        return job_id

    async def worker(self):
        while True:
            job_id = await self.job_queue.get()
            try:
                await self._process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.execute("UPDATE web_jobs SET status='failed',updated_at=?,error=? WHERE id=?",
                                (utc_text(), str(exc)[:500], job_id))
                self.db.commit()
            finally:
                self.job_queue.task_done()

    async def _process_job(self, job_id):
        self.check_available(require_destination=True)
        job = self.db.execute("SELECT * FROM web_jobs WHERE id=?", (job_id,)).fetchone()
        search = self.db.execute("SELECT * FROM web_searches WHERE id=?", (job["search_id"],)).fetchone()
        self.db.execute("UPDATE web_jobs SET status='running',updated_at=? WHERE id=?", (utc_text(), job_id))
        self.db.commit()
        async with self.telegram_lock:
            items = self.db.execute("SELECT * FROM web_job_items WHERE job_id=? ORDER BY option_number", (job_id,)).fetchall()
            for item in items:
                self.db.execute("UPDATE web_job_items SET status='downloading' WHERE job_id=? AND option_number=?",
                                (job_id, item["option_number"]))
                self.db.commit()
                try:
                    summary = await self._download_and_forward(search, item["option_number"])
                    self.db.execute("UPDATE web_job_items SET status='completed',result_json=? WHERE job_id=? AND option_number=?",
                                    (json.dumps(summary, ensure_ascii=False), job_id, item["option_number"]))
                except Exception as exc:
                    self.db.execute("UPDATE web_job_items SET status='failed',error=? WHERE job_id=? AND option_number=?",
                                    (str(exc)[:500], job_id, item["option_number"]))
                    self.db.execute("UPDATE web_jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                                    (str(exc)[:500], utc_text(), job_id))
                    self.db.commit()
                    return
                self.db.execute("UPDATE web_jobs SET updated_at=? WHERE id=?", (utc_text(), job_id))
                self.db.commit()
        self.db.execute("UPDATE web_jobs SET status='completed',updated_at=? WHERE id=?", (utc_text(), job_id))
        self.db.commit()

    async def _download_and_forward(self, search, option_number):
        if maintenance_cooldown_remaining(self.db):
            raise RuntimeError("O bot está em pausa de manutenção.")
        message = await self.client.get_messages(self.bot, ids=search["bot_message_id"])
        if not message or not getattr(message, "buttons", None):
            raise RuntimeError("Os resultados já não estão disponíveis no Telegram; faz nova pesquisa.")
        target = str(option_number)
        choice = None
        for row_index, row in enumerate(message.buttons):
            for column_index, button in enumerate(row):
                label = re.sub(r"\D", "", self._button_label(button))
                if label == target:
                    choice = (row_index, column_index)
                    break
            if choice:
                break
        if choice is None:
            raise RuntimeError(f"A opção {option_number} já não está disponível.")
        cursor = await self._latest_bot_message_id()
        try:
            answer = await self._click(message, *choice)
        except Exception as exc:
            raise RuntimeError(f"O Telegram não confirmou a opção {option_number} ({type(exc).__name__}).") from exc
        answer_text = getattr(answer, "message", None)
        if answer_text and "started" not in answer_text.casefold():
            raise RuntimeError(f"O bot recusou a opção {option_number}: {answer_text[:160]}")

        playlist_id = None
        expected = 0
        direct_track = False
        direct_track_media_at = None
        last_activity = asyncio.get_running_loop().time()
        deadline = last_activity + 20 * 60
        while asyncio.get_running_loop().time() < deadline:
            if maintenance_cooldown_remaining(self.db):
                raise RuntimeError("O bot entrou em pausa de manutenção.")
            messages = [item async for item in self.client.iter_messages(
                self.bot, min_id=cursor, reverse=True, limit=100)]
            if messages:
                for incoming in messages:
                    cursor = max(cursor, incoming.id)
                    last_activity = asyncio.get_running_loop().time()
                    text = incoming.message or ""
                    if is_maintenance_notice(text):
                        set_maintenance_cooldown(self.db)
                        raise RuntimeError("O bot informou manutenção; os novos pedidos estão suspensos por 30 minutos.")
                    start = is_playlist_start(text)
                    if start and playlist_id is None:
                        title, expected = start
                        playlist_id = start_playlist(self.db, self.bot.id, incoming.id, title, expected)
                        continue
                    if (playlist_id is None and search["content_type"] == "track"
                            and incoming.media and audio_metadata(incoming)):
                        selected = next((entry for entry in json.loads(search["results_json"])
                                         if entry["number"] == option_number), {})
                        synthetic_start_id = -(search["bot_message_id"] * 10 + option_number)
                        playlist_id = start_playlist(
                            self.db, self.bot.id, synthetic_start_id,
                            selected.get("title", f"Faixa {option_number}"), 1)
                        direct_track = True
                    if playlist_id is not None and incoming.media:
                        add_media(self.db, playlist_id, self.bot.id, incoming.id,
                                  media_document_id(incoming), audio_metadata(incoming))
                        if direct_track:
                            direct_track_media_at = asyncio.get_running_loop().time()
                    if playlist_id is not None and is_finished(text):
                        finished = finish_playlist(self.db, playlist_id, incoming.id)
                        if finished["note"]:
                            raise RuntimeError(f"A playlist ficou incompleta: {finished['note']}")
                        return await self._forward_playlist(playlist_id)
            else:
                current_time = asyncio.get_running_loop().time()
                if (direct_track and direct_track_media_at is not None
                        and current_time - direct_track_media_at >= 2):
                    finished = finish_playlist(self.db, playlist_id, cursor)
                    if finished["note"]:
                        raise RuntimeError(f"A faixa ficou incompleta: {finished['note']}")
                    return await self._forward_playlist(playlist_id)
                if current_time - last_activity > 120:
                    raise RuntimeError("O bot deixou de responder durante o descarregamento.")
            await asyncio.sleep(0.5)
        raise RuntimeError("O descarregamento excedeu o limite de 20 minutos; nada desta opção foi encaminhado.")

    async def _ensure_destination_index(self):
        if self.destination_indexed:
            return
        for entity in self.dedup_chats:
            history = [message async for message in self.client.iter_messages(
                entity, limit=None, filter=InputMessagesFilterMusic)]
            index_destination_media(self.db, entity.id, history)
        self.destination_indexed = True

    async def _forward_playlist(self, playlist_id):
        await self._ensure_destination_index()
        items = self.db.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY position",
                                (playlist_id,)).fetchall()
        chat_ids = [entity.id for entity in self.dedup_chats]
        placeholders = ",".join("?" for _ in chat_ids)
        existing_rows = self.db.execute(
            "SELECT DISTINCT document_id,title,performer,duration,file_size FROM destination_media WHERE destination_chat_id IN (" + placeholders + ")",
            tuple(chat_ids),
        ).fetchall() if chat_ids else []
        existing_docs = {row["document_id"] for row in existing_rows if row["document_id"]}
        existing_metadata = {
            (normalize(row["title"]), normalize(row["performer"]), row["duration"], row["file_size"])
            for row in existing_rows if row["title"] and row["performer"] and row["duration"] is not None
            and row["file_size"] is not None and row["file_size"] > 0
        }
        seen_docs, seen_metadata = set(), set()
        forwarded = duplicates = missing = 0
        for item in items:
            doc_id = item["document_id"]
            key = (normalize(item["title"]), normalize(item["performer"]), item["duration"], item["file_size"])
            has_key = bool(key[0] and key[1] and key[2] is not None and key[3] and key[3] > 0)
            if (doc_id and (doc_id in existing_docs or doc_id in seen_docs)) or (has_key and (key in existing_metadata or key in seen_metadata)):
                status = "duplicate"
                duplicates += 1
            else:
                source = await self.client.get_messages(self.bot, ids=item["source_message_id"])
                if not source or not source.media:
                    self.db.execute("UPDATE playlist_items SET status='missing',error=? WHERE playlist_id=? AND source_message_id=?",
                                    ("Mensagem original indisponível.", playlist_id, item["source_message_id"]))
                    self.db.commit()
                    missing += 1
                    continue
                sent = await self.client.forward_messages(self.destination, source)
                sent = sent[0] if isinstance(sent, list) else sent
                if not sent:
                    raise RuntimeError("O Telegram não confirmou um encaminhamento.")
                status = "forwarded"
                forwarded += 1
                if doc_id:
                    seen_docs.add(doc_id)
                    self.db.execute("""INSERT OR REPLACE INTO destination_media
                      (destination_chat_id,document_id,destination_message_id,seen_at,title,performer,duration,file_size)
                      VALUES(?,?,?,?,?,?,?,?)""",
                      (self.destination.id, doc_id, sent.id, utc_text(), item["title"],
                       item["performer"], item["duration"], item["file_size"]))
                if has_key:
                    seen_metadata.add(key)
            self.db.execute("UPDATE playlist_items SET status=? WHERE playlist_id=? AND source_message_id=?",
                            (status, playlist_id, item["source_message_id"]))
            self.db.commit()
        return {"forwarded": forwarded, "duplicates": duplicates,
                "missing": missing, "expected_tracks": len(items)}

    def get_jobs(self, username, job_id=None):
        if job_id:
            job = self.db.execute("SELECT * FROM web_jobs WHERE id=? AND username=?", (job_id, username)).fetchone()
            if not job:
                return None
            rows = self.db.execute("SELECT option_number,title,status,result_json,error FROM web_job_items WHERE job_id=? ORDER BY option_number",
                                   (job_id,)).fetchall()
            return {"id": job["id"], "status": job["status"], "created_at": job["created_at"],
                    "updated_at": job["updated_at"], "error": job["error"],
                    "items": [{"number": row["option_number"], "title": row["title"],
                               "status": row["status"],
                               "result": json.loads(row["result_json"]) if row["result_json"] else None,
                               "error": row["error"]} for row in rows]}
        jobs = self.db.execute("SELECT id FROM web_jobs WHERE username=? ORDER BY created_at DESC LIMIT 20", (username,)).fetchall()
        return [self.get_jobs(username, row["id"]) for row in jobs]

