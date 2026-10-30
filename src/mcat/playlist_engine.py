"""Playlist lifecycle and persistent deduplication helpers; no media downloads."""
import re
from datetime import datetime, timedelta, timezone

MAINTENANCE_COOLDOWN_SECONDS = 30 * 60

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def is_playlist_start(text):
    match = re.search(r"Total\s+Tracks\s*:\s*(\d+)", text or "", re.I)
    if not match:
        return None
    return ((text or "")[:match.start()].strip(" \n\r-—:") or "Untitled playlist", int(match.group(1)))

def is_finished(text):
    return bool(re.search(r"\bFinished\b", text or "", re.I))

def media_document_id(message):
    doc = getattr(message, "document", None)
    return str(doc.id) if doc is not None else None

def init_playlist_schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS playlists (
      id INTEGER PRIMARY KEY AUTOINCREMENT, source_chat_id INTEGER NOT NULL,
      start_message_id INTEGER NOT NULL, title TEXT NOT NULL, expected_tracks INTEGER NOT NULL,
      started_at TEXT NOT NULL, finished_message_id INTEGER, finished_at TEXT,
      observed_media INTEGER NOT NULL DEFAULT 0, unique_media INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'collecting', validation_note TEXT,
      UNIQUE(source_chat_id,start_message_id));
    CREATE TABLE IF NOT EXISTS playlist_items (
      playlist_id INTEGER NOT NULL, source_chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
      document_id TEXT, title TEXT, performer TEXT, duration INTEGER, file_size INTEGER,
      position INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      forwarded_message_id INTEGER, error TEXT, PRIMARY KEY(source_chat_id,source_message_id));
    CREATE TABLE IF NOT EXISTS destination_media (
      destination_chat_id INTEGER NOT NULL, document_id TEXT NOT NULL,
      destination_message_id INTEGER NOT NULL, seen_at TEXT NOT NULL,
      title TEXT, performer TEXT, duration INTEGER, file_size INTEGER,
      PRIMARY KEY(destination_chat_id,document_id,destination_message_id));
    CREATE TABLE IF NOT EXISTS runtime_state (
      key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    # Migrate databases created by earlier versions.
    for table in ("playlist_items", "destination_media"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, sql_type in (("title", "TEXT"), ("performer", "TEXT"), ("duration", "INTEGER"), ("file_size", "INTEGER")):
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    db.commit()

def start_playlist(db, chat_id, message_id, title, expected):
    db.execute("INSERT OR IGNORE INTO playlists(source_chat_id,start_message_id,title,expected_tracks,started_at,status) VALUES(?,?,?,?,?,'collecting')",
               (chat_id,message_id,title,expected,now_iso()))
    row=db.execute("SELECT id FROM playlists WHERE source_chat_id=? AND start_message_id=?",(chat_id,message_id)).fetchone()
    db.commit()
    return row["id"] if hasattr(row,"keys") else row[0]

def active_playlist(db, chat_id):
    return db.execute("SELECT * FROM playlists WHERE source_chat_id=? AND status='collecting' ORDER BY id DESC LIMIT 1",(chat_id,)).fetchone()

def add_media(db, playlist_id, chat_id, message_id, document_id, metadata=None):
    pos=db.execute("SELECT COALESCE(MAX(position),0)+1 FROM playlist_items WHERE playlist_id=?",(playlist_id,)).fetchone()[0]
    metadata = metadata or {}
    db.execute("INSERT OR IGNORE INTO playlist_items(playlist_id,source_chat_id,source_message_id,document_id,title,performer,duration,file_size,position,status) VALUES(?,?,?,?,?,?,?,?,?,'pending')",
               (playlist_id,chat_id,message_id,document_id,metadata.get("title"),metadata.get("performer"),metadata.get("duration"),metadata.get("file_size"),pos))
    db.commit()

def finish_playlist(db, playlist_id, message_id):
    p=db.execute("SELECT expected_tracks FROM playlists WHERE id=?",(playlist_id,)).fetchone()
    c=db.execute("SELECT COUNT(*) n,COUNT(DISTINCT COALESCE(document_id,'message:'||source_message_id)) u FROM playlist_items WHERE playlist_id=?",(playlist_id,)).fetchone()
    note=None if c["n"]==p["expected_tracks"] else f"Bot anunciou {p['expected_tracks']} faixas; foram observadas {c['n']} mensagens multimédia."
    db.execute("UPDATE playlists SET finished_message_id=?,finished_at=?,observed_media=?,unique_media=?,status=?,validation_note=? WHERE id=?",
               (message_id,now_iso(),c["n"],c["u"],"complete" if note is None else "complete_with_warning",note,playlist_id))
    db.commit()
    return {"expected":p["expected_tracks"],"observed":c["n"],"unique":c["u"],"note":note}


def interrupt_active_playlist(db, chat_id, note):
    """Close an abandoned collection without running its forwarding phase."""
    playlist = active_playlist(db, chat_id)
    if playlist is None:
        return None
    db.execute(
        "UPDATE playlists SET status='interrupted',finished_at=?,validation_note=? WHERE id=?",
        (now_iso(), note, playlist["id"]),
    )
    db.commit()
    return playlist["id"]


def interrupt_stale_playlists(db, note):
    """Prevent collections left open by a previous process from being reused."""
    cursor = db.execute(
        "UPDATE playlists SET status='interrupted',finished_at=?,validation_note=? WHERE status='collecting'",
        (now_iso(), note),
    )
    db.commit()
    return cursor.rowcount


def is_maintenance_notice(text):
    """Recognize maintenance responses from the external bot."""
    return bool(re.search(
        r"\bunder maintenance\b|\bmaintenance\b.*\b(?:paused|unavailable)\b|"
        r"\bdownloads?\s+(?:are\s+)?paused\b",
        text or "",
        re.I | re.S,
    ))


def set_maintenance_cooldown(db, now=None):
    """Persist a 30-minute request pause so a restart cannot clear it."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    until = current.astimezone(timezone.utc) + timedelta(seconds=MAINTENANCE_COOLDOWN_SECONDS)
    db.execute(
        "INSERT INTO runtime_state(key,value) VALUES('maintenance_until',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (until.isoformat(timespec="seconds"),),
    )
    db.commit()
    return until


def maintenance_cooldown_remaining(db, now=None):
    """Return remaining seconds; clear expired or invalid cooldown state."""
    row = db.execute(
        "SELECT value FROM runtime_state WHERE key='maintenance_until'"
    ).fetchone()
    if row is None:
        return 0
    value = row["value"] if hasattr(row, "keys") else row[0]
    try:
        until = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        db.execute("DELETE FROM runtime_state WHERE key='maintenance_until'")
        db.commit()
        return 0
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    remaining = (until - current.astimezone(timezone.utc)).total_seconds()
    if remaining <= 0:
        db.execute("DELETE FROM runtime_state WHERE key='maintenance_until'")
        db.commit()
        return 0
    return max(1, int(remaining))

def index_destination_media(db, chat_id, messages):
    count=0
    for message in messages:
        doc=media_document_id(message)
        if doc:
            metadata = audio_metadata(message) or {}
            # Telegram sometimes serves a message as a MessageService / wrapper;
            # keep missing attributes NULL rather than inventing duration.
            db.execute("""INSERT INTO destination_media
                (destination_chat_id,document_id,destination_message_id,seen_at,title,performer,duration,file_size)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(destination_chat_id,document_id,destination_message_id) DO UPDATE SET
                title=COALESCE(excluded.title,destination_media.title),
                performer=COALESCE(excluded.performer,destination_media.performer),
                duration=COALESCE(excluded.duration,destination_media.duration),
                file_size=COALESCE(excluded.file_size,destination_media.file_size)""",
                (chat_id,doc,message.id,now_iso(),metadata.get("title") or None,metadata.get("performer") or None,metadata.get("duration"),metadata.get("file_size")))
            count+=1
    db.commit()
    return count

def destination_has_media(db, chat_id, doc):
    return bool(doc and db.execute("SELECT 1 FROM destination_media WHERE destination_chat_id=? AND document_id=? LIMIT 1",(chat_id,doc)).fetchone())

def playlist_delivery_summary(db, playlist_id):
    """Summarize deduplication and delivery outcomes for a completed playlist."""
    rows = db.execute(
        "SELECT status, COUNT(*) AS n FROM playlist_items WHERE playlist_id=? GROUP BY status",
        (playlist_id,),
    ).fetchall()
    counts = {row["status"]: row["n"] for row in rows}
    duplicate = counts.get("duplicate", 0)
    playlist_duplicates = counts.get("duplicate_in_playlist", 0)
    forwarded = counts.get("forwarded", 0)
    blocked_after_error = counts.get("blocked_after_error", 0)
    errors = db.execute(
        "SELECT COUNT(*) AS n FROM playlist_items WHERE playlist_id=? AND (status='error' OR (status='forwarded' AND error IS NOT NULL))",
        (playlist_id,),
    ).fetchone()["n"]
    # Missing/error outcomes are still new items that were absent from the
    # destination index, but their delivery outcome is reported separately.
    new = sum(n for status, n in counts.items() if status not in ("duplicate", "duplicate_in_playlist"))
    return {
        "existing": duplicate,
        "playlist_duplicates": playlist_duplicates,
        "new": new,
        "forwarded": forwarded,
        "dry_run": counts.get("dry_run", 0),
        "limit_reached": counts.get("limit_reached", 0),
        "blocked_after_error": blocked_after_error,
        "missing": counts.get("missing", 0),
        "errors": errors,
    }


def audio_metadata(message):
    """Return lightweight Telegram audio metadata without downloading media."""
    document = getattr(message, "document", None)
    if document is None:
        return None
    attrs = getattr(document, "attributes", None) or []
    audio_attr = next(
        (attr for attr in attrs if attr.__class__.__name__ == "DocumentAttributeAudio" or hasattr(attr, "title") or hasattr(attr, "performer")),
        None,
    )
    mime_type = (getattr(document, "mime_type", None) or "").lower()
    if audio_attr is None and not mime_type.startswith("audio/"):
        return None
    title = getattr(audio_attr, "title", None) if audio_attr else None
    performer = getattr(audio_attr, "performer", None) if audio_attr else None
    return {
        "document_id": str(document.id),
        "message_id": getattr(message, "id", None),
        "title": (title or "").strip(),
        "performer": (performer or "").strip(),
        "duration": getattr(audio_attr, "duration", None) if audio_attr else None,
        "file_size": getattr(document, "size", None),
        "mime_type": mime_type,
    }


def norm_metadata(value):
    import unicodedata
    value = unicodedata.normalize("NFKD", (value or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def duplicate_groups(records):
    """Group exact Telegram documents and tracks with identical title, performer, duration and size."""
    from collections import defaultdict

    exact = defaultdict(list)
    metadata = defaultdict(list)
    for record in records:
        exact[record["document_id"]].append(record)
        title = norm_metadata(record.get("title"))
        performer = norm_metadata(record.get("performer"))
        duration = record.get("duration")
        file_size = record.get("file_size")
        if title and performer and duration is not None and file_size is not None and file_size > 0:
            metadata[(performer, title, duration, file_size)].append(record)
    exact_groups = [
        {"document_id": doc_id, "records": items}
        for doc_id, items in exact.items() if len(items) > 1
    ]
    metadata_groups = [
        {"performer": key[0], "title": key[1], "duration": key[2], "file_size": key[3], "records": items}
        for key, items in metadata.items()
        if len(items) > 1 and len({item["document_id"] for item in items}) > 1
    ]
    exact_groups.sort(key=lambda group: (-len(group["records"]), group["document_id"]))
    metadata_groups.sort(key=lambda group: (-len(group["records"]), group["performer"], group["title"], group["duration"], group["file_size"]))
    return exact_groups, metadata_groups


