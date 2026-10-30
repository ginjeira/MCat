import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from mcat.playlist_engine import add_media, active_playlist, audio_metadata, destination_has_media, duplicate_groups, finish_playlist, init_playlist_schema, interrupt_active_playlist, interrupt_stale_playlists, is_finished, is_maintenance_notice, is_playlist_start, maintenance_cooldown_remaining, playlist_delivery_summary, set_maintenance_cooldown, start_playlist

class PlaylistEngineTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.db.row_factory=sqlite3.Row
        init_playlist_schema(self.db)
    def tearDown(self):
        self.db.close()
    def test_start_end(self):
        self.assertEqual(is_playlist_start("Rock 80/90\nTotal Tracks: 25"),("Rock 80/90",25))
        self.assertIsNone(is_playlist_start("No results found"))
        self.assertTrue(is_finished("Download Finished"))
        self.assertFalse(is_finished("Downloading 4/25"))
    def test_collect_validate(self):
        pid=start_playlist(self.db,123,10,"Rock",2)
        add_media(self.db,pid,123,11,"doc-a")
        add_media(self.db,pid,123,12,"doc-b")
        result=finish_playlist(self.db,pid,13)
        self.assertEqual(result["observed"],2)
        self.assertIsNone(result["note"])
        self.assertIsNone(active_playlist(self.db,123))
    def test_mismatch(self):
        pid=start_playlist(self.db,123,10,"Rock",3)
        add_media(self.db,pid,123,11,"doc-a")
        self.assertIn("anunciou 3",finish_playlist(self.db,pid,12)["note"])
    def test_interrupt_closes_incomplete_collection(self):
        pid=start_playlist(self.db,123,10,"Rock",2)
        self.assertEqual(interrupt_active_playlist(self.db,123,"timeout"),pid)
        self.assertIsNone(active_playlist(self.db,123))
        row=self.db.execute("SELECT status,validation_note FROM playlists WHERE id=?",(pid,)).fetchone()
        self.assertEqual((row["status"],row["validation_note"]),("interrupted","timeout"))
        self.assertIsNone(interrupt_active_playlist(self.db,123,"timeout"))
    def test_startup_recovery_closes_all_stale_collections(self):
        first=start_playlist(self.db,123,10,"Rock",2)
        second=start_playlist(self.db,456,20,"Pop",1)
        self.assertEqual(interrupt_stale_playlists(self.db,"restart"),2)
        self.assertIsNone(active_playlist(self.db,123))
        self.assertIsNone(active_playlist(self.db,456))
        states={row["id"]:row["status"] for row in self.db.execute("SELECT id,status FROM playlists")}
        self.assertEqual(states,{first:"interrupted",second:"interrupted"})
    def test_maintenance_notice_detection(self):
        self.assertTrue(is_maintenance_notice("The bot is under maintenance, so new downloads are paused for a few minutes."))
        self.assertTrue(is_maintenance_notice("New downloads are paused."))
        self.assertFalse(is_maintenance_notice("Searching for 'Pop All Stars' in 'playlist' from 'deezer'."))
    def test_maintenance_cooldown_survives_restart_and_expires(self):
        now=datetime(2026,10,30,16,0,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"mcat.sqlite3"
            first=sqlite3.connect(path)
            first.row_factory=sqlite3.Row
            init_playlist_schema(first)
            set_maintenance_cooldown(first,now=now)
            first.close()

            second=sqlite3.connect(path)
            second.row_factory=sqlite3.Row
            init_playlist_schema(second)
            self.assertEqual(maintenance_cooldown_remaining(second,now=now),1800)
            self.assertEqual(maintenance_cooldown_remaining(second,now=now+timedelta(minutes=29,seconds=59)),1)
            self.assertEqual(maintenance_cooldown_remaining(second,now=now+timedelta(minutes=30)),0)
            second.close()
    def test_delivery_summary(self):
        pid=start_playlist(self.db,123,10,"Pop",3)
        add_media(self.db,pid,123,11,"doc-a")
        add_media(self.db,pid,123,12,"doc-b")
        add_media(self.db,pid,123,13,"doc-c")
        add_media(self.db,pid,123,14,"doc-a")
        add_media(self.db,pid,123,15,"doc-d")
        add_media(self.db,pid,123,16,"doc-e")
        self.db.execute("UPDATE playlist_items SET status='duplicate' WHERE playlist_id=? AND source_message_id=11",(pid,))
        self.db.execute("UPDATE playlist_items SET status='dry_run' WHERE playlist_id=? AND source_message_id IN (12,13)",(pid,))
        self.db.execute("UPDATE playlist_items SET status='duplicate_in_playlist' WHERE playlist_id=? AND source_message_id=14",(pid,))
        self.db.execute("UPDATE playlist_items SET status='blocked_after_error' WHERE playlist_id=? AND source_message_id=15",(pid,))
        self.db.execute("UPDATE playlist_items SET status='forwarded',error='ledger update failed' WHERE playlist_id=? AND source_message_id=16",(pid,))
        self.db.commit()
        result=playlist_delivery_summary(self.db,pid)
        self.assertEqual(result,{"existing":1,"playlist_duplicates":1,"new":4,"forwarded":1,"dry_run":2,"limit_reached":0,"blocked_after_error":1,"missing":0,"errors":1})

    def test_dedup(self):
        self.db.execute("INSERT INTO destination_media(destination_chat_id,document_id,destination_message_id,seen_at) VALUES(?,?,?,?)",(456,"doc-a",88,"now"))
        self.db.commit()
        self.assertTrue(destination_has_media(self.db,456,"doc-a"))
        self.assertFalse(destination_has_media(self.db,456,"doc-b"))



class FakeAudioAttribute:
    def __init__(self, title=None, performer=None, duration=180):
        self.title = title
        self.performer = performer
        self.duration = duration


class FakeDocument:
    def __init__(self, doc_id, mime_type="audio/mpeg", attributes=None, size=4096):
        self.id = doc_id
        self.mime_type = mime_type
        self.attributes = attributes or []
        self.size = size


class FakeMessage:
    def __init__(self, message_id, document):
        self.id = message_id
        self.document = document


class DuplicateAuditTests(unittest.TestCase):
    def test_audio_metadata_ignores_non_audio_documents(self):
        self.assertIsNone(audio_metadata(FakeMessage(1, FakeDocument(10, "application/pdf"))))

    def test_audio_metadata_extracts_telegram_audio_attributes(self):
        msg = FakeMessage(7, FakeDocument(123, attributes=[FakeAudioAttribute("Song", "Artist")]))
        self.assertEqual(audio_metadata(msg), {
            "document_id": "123", "message_id": 7, "title": "Song",
            "performer": "Artist", "duration": 180, "file_size": 4096,
            "mime_type": "audio/mpeg",
        })

    def test_duplicate_groups_separates_exact_and_metadata_matches(self):
        records = [
            {"document_id": "a", "message_id": 1, "title": "Song", "performer": "Artist", "duration": 180, "file_size": 4096},
            {"document_id": "a", "message_id": 2, "title": "Song", "performer": "Artist", "duration": 180, "file_size": 4096},
            {"document_id": "b", "message_id": 3, "title": " song ", "performer": "ARTIST", "duration": 180, "file_size": 4096},
            {"document_id": "c", "message_id": 4, "title": "", "performer": "Artist"},
        ]
        exact, metadata = duplicate_groups(records)
        self.assertEqual([group["document_id"] for group in exact], ["a"])
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0]["title"], "song")
        self.assertEqual(len(metadata[0]["records"]), 3)


if __name__ == "__main__":
    unittest.main()


