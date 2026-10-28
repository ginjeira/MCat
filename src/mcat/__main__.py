import asyncio
import csv
import os
import re
import shlex
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()

API_ID_RAW = os.getenv("TG_API_ID", "").strip()
API_HASH = os.getenv("TG_API_HASH", "").strip()
BOT_USERNAME = os.getenv("TG_BOT_USERNAME", "").strip().lstrip("@")
DESTINATION = os.getenv("TG_DESTINATION", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "./data/mcat.sqlite3")).expanduser()

if not API_ID_RAW or not API_HASH or not BOT_USERNAME:
    raise SystemExit("Configuração em falta: preenche TG_API_ID, TG_API_HASH e TG_BOT_USERNAME em .env")
try:
    API_ID = int(API_ID_RAW)
except ValueError:
    raise SystemExit("TG_API_ID tem de ser um número inteiro.")

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
client = TelegramClient("mcat_user_session", API_ID, API_HASH)
current_query = ""
current_label = "sem-etiqueta"
forwarding_armed = False
bot_entity = None
destination_entity = None


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(value):
    value = unicodedata.normalize("NFKD", (value or "").casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


def init_db():
    db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS observations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      seen_at TEXT NOT NULL,
      source_chat_id INTEGER NOT NULL,
      source_message_id INTEGER NOT NULL,
      grouped_id TEXT,
      message_text TEXT,
      has_media INTEGER NOT NULL DEFAULT 0,
      media_kind TEXT,
      document_id TEXT,
      query_text TEXT,
      label TEXT,
      dedup_key TEXT,
      duplicate_of INTEGER,
      forwarded INTEGER NOT NULL DEFAULT 0,
      destination_chat_id INTEGER,
      destination_message_ids TEXT,
      error TEXT,
      UNIQUE(source_chat_id, source_message_id)
    );
    CREATE INDEX IF NOT EXISTS idx_observations_dedup ON observations(dedup_key);
    CREATE INDEX IF NOT EXISTS idx_observations_doc ON observations(document_id);
    """)
    db.commit()


def media_kind(message):
    # Apenas classifica a mensagem Telegram; nunca descarrega nem lê o conteúdo do ficheiro.
    if not message.media:
        return None, None
    if message.photo:
        return "photo", None
    if message.document:
        doc_id = str(message.document.id)
        mime = getattr(message.file, "mime_type", None) if message.file else None
        if mime and mime.startswith("audio/"):
            return "audio-message", doc_id
        return "document-or-other-media", doc_id
    return "other-media", None


def save_observation(message, forwarded=False, dest_id=None, dest_message_ids=None, error=None):
    text = message.message or ""
    kind, doc_id = media_kind(message)
    # Heurística só para texto: não identifica com segurança todas as versões de uma música.
    key = norm(text) if text.strip() else None
    duplicate_of = None
    if key:
        row = db.execute("SELECT id FROM observations WHERE dedup_key=? ORDER BY id LIMIT 1", (key,)).fetchone()
        if row:
            duplicate_of = row["id"]
    db.execute("""INSERT INTO observations
      (seen_at, source_chat_id, source_message_id, grouped_id, message_text, has_media,
       media_kind, document_id, query_text, label, dedup_key, duplicate_of, forwarded,
       destination_chat_id, destination_message_ids, error)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(source_chat_id, source_message_id) DO UPDATE SET
       forwarded=excluded.forwarded,
       destination_chat_id=excluded.destination_chat_id,
       destination_message_ids=excluded.destination_message_ids,
       error=excluded.error""",
      (now_iso(), message.chat_id, message.id, str(message.grouped_id) if message.grouped_id else None,
       text, int(bool(message.media)), kind, doc_id, current_query, current_label, key,
       duplicate_of, int(forwarded), dest_id, dest_message_ids, error))
    db.commit()


def print_status():
    total = db.execute("SELECT COUNT(*) n FROM observations").fetchone()["n"]
    media = db.execute("SELECT COUNT(*) n FROM observations WHERE has_media=1").fetchone()["n"]
    forwarded = db.execute("SELECT COUNT(*) n FROM observations WHERE forwarded=1").fetchone()["n"]
    duplicates = db.execute("SELECT COUNT(*) n FROM observations WHERE duplicate_of IS NOT NULL").fetchone()["n"]
    errors = db.execute("SELECT COUNT(*) n FROM observations WHERE error IS NOT NULL").fetchone()["n"]
    print(f"Mensagens observadas: {total} | com media: {media} | encaminhadas: {forwarded} | possíveis duplicados de texto: {duplicates} | erros: {errors}")


def export_csv(path):
    rows = db.execute("SELECT * FROM observations ORDER BY id").fetchall()
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else ["id", "seen_at", "source_chat_id", "source_message_id", "message_text", "label", "forwarded"]
    with out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([dict(row) for row in rows])
    print(f"Exportados {len(rows)} registos para {out}")


async def main():
    global current_query, current_label, forwarding_armed, bot_entity, destination_entity
    init_db()
    await client.start()
    bot_entity = await client.get_entity(BOT_USERNAME)
    if DESTINATION:
        try:
            destination_entity = await client.get_entity(DESTINATION)
        except Exception as exc:
            print(f"Aviso: não consegui resolver TG_DESTINATION ({exc}). Configura o destino corretamente.")
    print(f"MCat ligado. Bot observado: @{BOT_USERNAME}")
    print("Modo inicial: encaminhamento DESARMADO. Não são descarregados nem processados ficheiros de áudio.")
    print("Usa /help para comandos. Para encaminhar, configura TG_DESTINATION e usa /arm antes de selecionar uma playlist no Telegram.\n")

    @client.on(events.NewMessage(chats=bot_entity))
    async def on_bot_message(event):
        message = event.message
        forwarded = False
        dest_id = None
        dest_ids = None
        error = None
        if forwarding_armed:
            if destination_entity is None:
                error = "TG_DESTINATION não está configurado ou não foi resolvido"
                print(f"\n[ERRO] msg={message.id}: {error}")
            else:
                try:
                    # Encaminha a mensagem Telegram original. Nenhum ficheiro é descarregado.
                    result = await client.forward_messages(destination_entity, message)
                    result_list = result if isinstance(result, list) else [result]
                    forwarded = True
                    dest_id = destination_entity.id
                    dest_ids = ",".join(str(item.id) for item in result_list if item)
                    print(f"\n[FORWARD] mensagem {message.id} -> destino {dest_id} (mensagens destino: {dest_ids})")
                except Exception as exc:
                    error = str(exc)[:500]
                    print(f"\n[ERRO FORWARD] mensagem {message.id}: {error}")
        save_observation(message, forwarded, dest_id, dest_ids, error)
        print("> ", end="", flush=True)

    while True:
        try:
            raw = await asyncio.to_thread(input, "> ")
        except (EOFError, KeyboardInterrupt):
            break
        try:
            parts = shlex.split(raw)
        except ValueError as exc:
            print(f"Comando inválido: {exc}")
            continue
        if not parts:
            continue
        command, args = parts[0].lower(), parts[1:]
        if command in ("/quit", "/exit"):
            forwarding_armed = False
            break
        if command == "/help":
            print("/search <termo>   envia uma pesquisa ao bot\n/tag <etiqueta>   etiqueta as mensagens seguintes\n/arm             ativa encaminhamento automático das próximas mensagens do bot\n/disarm           desativa encaminhamento\n/status           estatísticas\n/export <ficheiro.csv> exportação CSV\n/help             ajuda\n/quit             sair")
        elif command == "/search":
            if not args:
                print("Uso: /search <termo>")
                continue
            current_query = " ".join(args)
            current_label = norm(current_query).replace(" ", "-") or "sem-etiqueta"
            await client.send_message(bot_entity, current_query)
            print("Pesquisa enviada. Seleciona manualmente o modo Playlist e o resultado no Telegram.")
        elif command == "/tag":
            current_label = "-".join(norm(" ".join(args)).split()) if args else "sem-etiqueta"
            print(f"Etiqueta atual: {current_label}")
        elif command == "/arm":
            if destination_entity is None:
                print("Encaminhamento não ativado: configura TG_DESTINATION em .env e reinicia a aplicação.")
                continue
            forwarding_armed = True
            print("ENCAMINHAMENTO ATIVO. As próximas mensagens recebidas do bot serão encaminhadas. Usa /disarm quando terminar.")
        elif command == "/disarm":
            forwarding_armed = False
            print("Encaminhamento desativado.")
        elif command == "/status":
            print_status()
        elif command == "/export":
            if not args:
                print("Uso: /export <ficheiro.csv>")
                continue
            export_csv(args[0])
        else:
            print("Comando desconhecido. Usa /help.")
    forwarding_armed = False
    await client.disconnect()
    db.close()
    print("MCat terminado.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
