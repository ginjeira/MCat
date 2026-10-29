"""Playlist lifecycle and persistent deduplication helpers; no media downloads."""
import re
from datetime import datetime, timezone

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
      document_id TEXT, position INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      forwarded_message_id INTEGER, error TEXT, PRIMARY KEY(source_chat_id,source_message_id));
    CREATE TABLE IF NOT EXISTS destination_media (
      destination_chat_id INTEGER NOT NULL, document_id TEXT NOT NULL,
      destination_message_id INTEGER NOT NULL, seen_at TEXT NOT NULL,
      PRIMARY KEY(destination_chat_id,document_id,destination_message_id));
    """)
    db.commit()

def start_playlist(db, chat_id, message_id, title, expected):
    db.execute("INSERT OR IGNORE INTO playlists(source_chat_id,start_message_id,title,expected_tracks,started_at,status) VALUES(?,?,?,?,?,'collecting')",
               (chat_id,message_id,title,expected,now_iso()))
    row=db.execute("SELECT id FROM playlists WHERE source_chat_id=? AND start_message_id=?",(chat_id,message_id)).fetchone()
    db.commit()
    return row["id"] if hasattr(row,"keys") else row[0]

def active_playlist(db, chat_id):
    return db.execute("SELECT * FROM playlists WHERE source_chat_id=? AND status='collecting' ORDER BY id DESC LIMIT 1",(chat_id,)).fetchone()

def add_media(db, playlist_id, chat_id, message_id, document_id):
    pos=db.execute("SELECT COALESCE(MAX(position),0)+1 FROM playlist_items WHERE playlist_id=?",(playlist_id,)).fetchone()[0]
    db.execute("INSERT OR IGNORE INTO playlist_items(playlist_id,source_chat_id,source_message_id,document_id,position,status) VALUES(?,?,?,?,?,'pending')",
               (playlist_id,chat_id,message_id,document_id,pos))
    db.commit()

def finish_playlist(db, playlist_id, message_id):
    p=db.execute("SELECT expected_tracks FROM playlists WHERE id=?",(playlist_id,)).fetchone()
    c=db.execute("SELECT COUNT(*) n,COUNT(DISTINCT COALESCE(document_id,'message:'||source_message_id)) u FROM playlist_items WHERE playlist_id=?",(playlist_id,)).fetchone()
    note=None if c["n"]==p["expected_tracks"] else f"Bot anunciou {p['expected_tracks']} faixas; foram observadas {c['n']} mensagens multimédia."
    db.execute("UPDATE playlists SET finished_message_id=?,finished_at=?,observed_media=?,unique_media=?,status=?,validation_note=? WHERE id=?",
               (message_id,now_iso(),c["n"],c["u"],"complete" if note is None else "complete_with_warning",note,playlist_id))
    db.commit()
    return {"expected":p["expected_tracks"],"observed":c["n"],"unique":c["u"],"note":note}

def index_destination_media(db, chat_id, messages):
    count=0
    for message in messages:
        doc=media_document_id(message)
        if doc:
            db.execute("INSERT OR IGNORE INTO destination_media VALUES(?,?,?,?)",(chat_id,doc,message.id,now_iso()))
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
    forwarded = counts.get("forwarded", 0)
    # Every processed non-duplicate item was absent from the destination index
    # when the playlist was evaluated. This includes failures/missing source
    # messages, which remain visible in the separate outcome status counts.
    new = sum(n for status, n in counts.items() if status != "duplicate")
    return {
        "existing": duplicate,
        "new": new,
        "forwarded": forwarded,
        "dry_run": counts.get("dry_run", 0),
        "missing": counts.get("missing", 0),
        "errors": counts.get("error", 0),
    }
