import asyncio
import csv
import os
import re
import shlex
import sqlite3
import unicodedata

from .playlist_engine import add_media, active_playlist, audio_metadata, destination_has_media, duplicate_groups, finish_playlist, index_destination_media, init_playlist_schema, is_finished, is_playlist_start, media_document_id, playlist_delivery_summary, start_playlist
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.functions.messages import SearchRequest
from telethon.tl.types import InputMessagesFilterMusic

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
    init_playlist_schema(db)
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
            destination_ref = int(DESTINATION) if DESTINATION.lstrip("-").isdigit() else DESTINATION
            destination_entity = await client.get_entity(destination_ref)
        except Exception as exc:
            print(f"Aviso: não consegui resolver TG_DESTINATION ({exc}). Configura o destino corretamente.")
    print(f"MCat ligado. Bot observado: @{BOT_USERNAME}")
    print("Modo inicial: encaminhamento DESARMADO. Não são descarregados nem processados ficheiros de áudio.")
    print("Usa /help para comandos. Para encaminhar, configura TG_DESTINATION e usa /arm antes de selecionar uma playlist no Telegram.\n")

    dry_run = True
    destination_indexed = False

    @client.on(events.NewMessage(chats=bot_entity))
    async def on_bot_message(event):
        nonlocal dry_run, destination_indexed
        message = event.message
        text = message.message or ""
        if not forwarding_armed:
            save_observation(message)
            return
        start = is_playlist_start(text)
        if start:
            title, expected = start
            pid = start_playlist(db, message.chat_id, message.id, title, expected)
            print(f"\n[PLAYLIST] Início: {title!r}; Total Tracks={expected}; id={pid}")
            save_observation(message)
            print("> ", end="", flush=True)
            return
        active = active_playlist(db, message.chat_id)
        if active and is_finished(text):
            summary = finish_playlist(db, active["id"], message.id)
            print(f"\n[PLAYLIST] Finished. Esperadas={summary['expected']}; observadas={summary['observed']}; únicas={summary['unique']}.")
            if summary["note"]:
                print(f"[VALIDAÇÃO] Aviso: {summary['note']}")
            if destination_entity is None:
                print("[DRY-RUN] Destino não resolvido; nada foi enviado.")
            else:
                if not destination_indexed:
                    print("[DEDUP] A indexar histórico de GinjMusic (sem descarregar ficheiros)...")
                    index_destination_media(db, destination_entity.id, [m async for m in client.iter_messages(destination_entity, limit=None, filter=InputMessagesFilterMusic)])
                    destination_indexed = True
                items=db.execute("SELECT * FROM playlist_items WHERE playlist_id=? ORDER BY position",(active["id"],)).fetchall()
                # Snapshot do destino antes de processar esta playlist, para que
                # um envio durante o ciclo não passe a contar como preexistente.
                existing_ids = {
                    row["document_id"] for row in db.execute(
                        "SELECT DISTINCT document_id FROM destination_media WHERE destination_chat_id=?",
                        (destination_entity.id,),
                    ).fetchall()
                }
                seen_playlist_ids = set()
                for item in items:
                    doc_id = item["document_id"]
                    if doc_id and doc_id in existing_ids:
                        db.execute("UPDATE playlist_items SET status='duplicate' WHERE playlist_id=? AND source_message_id=?",(active["id"],item["source_message_id"]))
                    elif doc_id and doc_id in seen_playlist_ids:
                        db.execute("UPDATE playlist_items SET status='duplicate_in_playlist' WHERE playlist_id=? AND source_message_id=?",(active["id"],item["source_message_id"]))
                    elif dry_run:
                        if doc_id:
                            seen_playlist_ids.add(doc_id)
                        db.execute("UPDATE playlist_items SET status='dry_run' WHERE playlist_id=? AND source_message_id=?",(active["id"],item["source_message_id"]))
                    else:
                        if doc_id:
                            seen_playlist_ids.add(doc_id)
                        try:
                            source=await client.get_messages(bot_entity,ids=item["source_message_id"])
                            if not source or not source.media:
                                db.execute("UPDATE playlist_items SET status='missing',error='Mensagem indisponível' WHERE playlist_id=? AND source_message_id=?",(active["id"],item["source_message_id"]))
                            else:
                                sent=await client.forward_messages(destination_entity,source)
                                sent=sent[0] if isinstance(sent,list) else sent
                                db.execute("UPDATE playlist_items SET status='forwarded',forwarded_message_id=? WHERE playlist_id=? AND source_message_id=?",(getattr(sent,"id",None),active["id"],item["source_message_id"]))
                                save_observation(source, forwarded=True, dest_id=destination_entity.id, dest_message_ids=str(getattr(sent,"id","")))
                                if item["document_id"] and sent:
                                    db.execute("INSERT OR IGNORE INTO destination_media VALUES(?,?,?,?)",(destination_entity.id,item["document_id"],sent.id,now_iso()))
                        except Exception as exc:
                            db.execute("UPDATE playlist_items SET status='error',error=? WHERE playlist_id=? AND source_message_id=?",(str(exc)[:500],active["id"],item["source_message_id"]))
                    db.commit()
            delivery = playlist_delivery_summary(db, active["id"])
            print(
                f"[DEDUP] Já existentes em GinjMusic={delivery['existing']}; "
                f"repetidas dentro da playlist={delivery['playlist_duplicates']}; "
                f"novas (não existentes antes do processamento)={delivery['new']}."
            )
            print(
                f"[RESULTADO] Encaminhadas={delivery['forwarded']}; "
                f"simulação={delivery['dry_run']}; "
                f"mensagens em falta={delivery['missing']}; erros={delivery['errors']}."
            )
            print("[PLAYLIST] Concluída em modo simulação." if dry_run else "[PLAYLIST] Processamento real terminado.")
            dry_run = True  # autorização real aplica-se apenas a uma playlist
            save_observation(message)
            print("> ", end="", flush=True)
            return
        active=active_playlist(db,message.chat_id)
        if active and message.media:
            add_media(db,active["id"],message.chat_id,message.id,media_document_id(message))
        save_observation(message)
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
            print("/search <termo>   envia uma pesquisa ao bot\n/tag <etiqueta>   etiqueta as mensagens seguintes\n/arm             recolhe playlist em simulação\n/live            autoriza encaminhamento real\n/disarm           desativa recolha\n/music-check      indexa histórico de música de GinjMusic\n/music-audit      compara filtro Music com histórico completo\n/music-search-audit compara paginação SearchRequest com iter_messages\n/duplicates       audita duplicados de áudio (só leitura)\n/status           estatísticas\n/export <ficheiro.csv> exportação CSV\n/help             ajuda\n/quit             sair")
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
            forwarding_armed = True
            print("RECOLHA ATIVA. Aguarda Total Tracks e Finished; modo simulação por omissão.")
        elif command == "/live":
            if destination_entity is None or not forwarding_armed:
                print("Para modo real, configura o destino e usa /arm.")
                continue
            dry_run = False
            print("MODO REAL: as faixas novas serão encaminhadas quando chegar Finished.")
        elif command == "/music-check":
            if destination_entity is None:
                print("Destino não resolvido; verifica TG_DESTINATION.")
                continue
            count = index_destination_media(db,destination_entity.id,[m async for m in client.iter_messages(destination_entity,limit=None,filter=InputMessagesFilterMusic)])
            destination_indexed = True
            total = db.execute("SELECT COUNT(DISTINCT document_id) n FROM destination_media WHERE destination_chat_id=?",(destination_entity.id,)).fetchone()["n"]
            print(f"Indexadas {count} mensagens multimédia; {total} identificadores distintos. A API consulta o histórico, não a vista Music da aplicação.")
        elif command == "/music-audit":
            if destination_entity is None:
                print("Destino não resolvido; verifica TG_DESTINATION.")
                continue
            print("[MUSIC-AUDIT] A comparar filtro Music com o histórico completo; só metadados, sem downloads...")
            filtered_ids = set()
            async for item in client.iter_messages(destination_entity, limit=None, filter=InputMessagesFilterMusic):
                filtered_ids.add(item.id)
            audio_records = []
            scanned = 0
            async for item in client.iter_messages(destination_entity, limit=None):
                scanned += 1
                record = audio_metadata(item)
                if record:
                    audio_records.append(record)
            audio_by_id = {record["message_id"]: record for record in audio_records}
            audio_ids = set(audio_by_id)
            audio_not_in_filter = sorted(audio_ids - filtered_ids, reverse=True)
            filter_not_detected_as_audio = sorted(filtered_ids - audio_ids, reverse=True)
            print(f"[MUSIC-AUDIT] Mensagens totais analisadas={scanned}.")
            print(f"[MUSIC-AUDIT] Itens devolvidos pelo filtro Music={len(filtered_ids)}.")
            print(f"[MUSIC-AUDIT] Mensagens com atributos de áudio/MIME audio identificadas no histórico completo={len(audio_ids)}.")
            print(f"[MUSIC-AUDIT] Áudios detetados fora do filtro Music={len(audio_not_in_filter)}.")
            for message_id in audio_not_in_filter[:30]:
                record = audio_by_id[message_id]
                label = " — ".join(part for part in (record.get("performer"), record.get("title")) if part) or "(sem título/intérprete)"
                print(f"  [FORA DO FILTRO] mensagem={message_id}; {label}; MIME={record.get('mime_type') or 'desconhecido'}")
            if len(audio_not_in_filter) > 30:
                print(f"  ... mais {len(audio_not_in_filter) - 30} mensagens de áudio não apresentadas.")
            print(f"[MUSIC-AUDIT] Itens do filtro sem áudio identificado pelo nosso extrator={len(filter_not_detected_as_audio)}.")
            if filter_not_detected_as_audio:
                print("  IDs de exemplo: " + ", ".join(str(mid) for mid in filter_not_detected_as_audio[:30]))
            print("[MUSIC-AUDIT] Auditoria apenas de leitura; nenhuma mensagem foi alterada.")
        elif command == "/music-search-audit":
            if destination_entity is None:
                print("Destino não resolvido; verifica TG_DESTINATION.")
                continue
            print("[MUSIC-SEARCH-AUDIT] A comparar iter_messages com SearchRequest; só leitura, sem downloads...")
            iter_ids = set()
            async for item in client.iter_messages(destination_entity, limit=None, filter=InputMessagesFilterMusic):
                iter_ids.add(item.id)
            search_ids = set()
            offset_id = 0
            page_number = 0
            page_size = 100
            while True:
                page_number += 1
                result = await client(SearchRequest(
                    peer=destination_entity,
                    q="",
                    filter=InputMessagesFilterMusic(),
                    min_date=None,
                    max_date=None,
                    offset_id=offset_id,
                    add_offset=0,
                    limit=page_size,
                    max_id=0,
                    min_id=0,
                    hash=0,
                ))
                messages = [item for item in result.messages if getattr(item, "id", None)]
                page_ids = {item.id for item in messages}
                new_ids = page_ids - search_ids
                print(f"[MUSIC-SEARCH-AUDIT] Página={page_number}; mensagens recebidas={len(messages)}; novos IDs={len(new_ids)}.")
                if not messages or not new_ids:
                    break
                search_ids.update(page_ids)
                next_offset = min(page_ids)
                if next_offset == offset_id:
                    print("[MUSIC-SEARCH-AUDIT] Paragem de segurança: offset não avançou.")
                    break
                offset_id = next_offset
                if len(messages) < page_size:
                    break
            only_search = sorted(search_ids - iter_ids, reverse=True)
            only_iter = sorted(iter_ids - search_ids, reverse=True)
            overlap = len(search_ids & iter_ids)
            print(f"[MUSIC-SEARCH-AUDIT] Total iter_messages(filter=Music)={len(iter_ids)} IDs únicos.")
            print(f"[MUSIC-SEARCH-AUDIT] Total SearchRequest(filter=Music)={len(search_ids)} IDs únicos em {page_number} páginas.")
            print(f"[MUSIC-SEARCH-AUDIT] IDs comuns={overlap}.")
            print(f"[MUSIC-SEARCH-AUDIT] Apenas SearchRequest={len(only_search)}.")
            if only_search:
                print("  IDs de exemplo (SearchRequest): " + ", ".join(str(mid) for mid in only_search[:40]))
            print(f"[MUSIC-SEARCH-AUDIT] Apenas iter_messages={len(only_iter)}.")
            if only_iter:
                print("  IDs de exemplo (iter_messages): " + ", ".join(str(mid) for mid in only_iter[:40]))
            print("[MUSIC-SEARCH-AUDIT] Auditoria apenas de leitura; nenhuma mensagem foi alterada.")
        elif command == "/duplicates":
            if destination_entity is None:
                print("Destino não resolvido; verifica TG_DESTINATION.")
                continue
            print("[DUPLICATES] A analisar o histórico acessível de GinjMusic (só metadados; sem descarregar ficheiros)...")
            records = []
            scanned = 0
            async for history_message in client.iter_messages(destination_entity, limit=None, filter=InputMessagesFilterMusic):
                scanned += 1
                record = audio_metadata(history_message)
                if record:
                    records.append(record)
            exact_groups, metadata_groups = duplicate_groups(records)
            exact_occurrences = sum(len(group["records"]) - 1 for group in exact_groups)
            print(f"[DUPLICATES] Mensagens analisadas={scanned}; ficheiros de áudio identificados={len(records)}.")
            print(f"[DUPLICATES] Repetições exatas do mesmo documento Telegram={len(exact_groups)} grupos ({exact_occurrences} ocorrências adicionais).")
            for group in exact_groups[:25]:
                first = group["records"][0]
                label = " — ".join(part for part in (first.get("performer"), first.get("title")) if part) or "(sem título/intérprete)"
                ids = ", ".join(str(item["message_id"]) for item in group["records"])
                print(f"  [FICHEIRO REPETIDO] {label}; mensagens={ids}; document_id={group['document_id']}")
            if len(exact_groups) > 25:
                print(f"  ... mais {len(exact_groups) - 25} grupos de documentos repetidos não apresentados.")
            print(f"[DUPLICATES] Possíveis duplicados por título/intérprete={len(metadata_groups)} grupos (podem ser ficheiros diferentes da mesma música).")
            for group in metadata_groups[:25]:
                label = " — ".join(part for part in (group["performer"], group["title"]) if part) or "(sem título)"
                ids = ", ".join(str(item["message_id"]) for item in group["records"])
                print(f"  [POSSÍVEL DUPLICADO] {label}; mensagens={ids}; ficheiros distintos={len({item['document_id'] for item in group['records']})}")
            if len(metadata_groups) > 25:
                print(f"  ... mais {len(metadata_groups) - 25} grupos por metadados não apresentados.")
            if not exact_groups and not metadata_groups:
                print("[DUPLICATES] Não foram encontrados grupos de duplicados com os metadados disponíveis.")
            print("[DUPLICATES] Auditoria apenas de leitura; nenhuma mensagem foi alterada ou apagada.")
        elif command == "/disarm":
            forwarding_armed = False
            print("Recolha desativada. O processamento termina quando chega Finished.")
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
