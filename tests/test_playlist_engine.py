import sqlite3
import unittest
from mcat.playlist_engine import add_media, active_playlist, audio_metadata, destination_has_media, duplicate_groups, finish_playlist, init_playlist_schema, is_finished, is_playlist_start, playlist_delivery_summary, start_playlist

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
    def test_delivery_summary(self):
        pid=start_playlist(self.db,123,10,"Pop",3)
        add_media(self.db,pid,123,11,"doc-a")
        add_media(self.db,pid,123,12,"doc-b")
        add_media(self.db,pid,123,13,"doc-c")
        add_media(self.db,pid,123,14,"doc-a")
        self.db.execute("UPDATE playlist_items SET status='duplicate' WHERE playlist_id=? AND source_message_id=11",(pid,))
        self.db.execute("UPDATE playlist_items SET status='dry_run' WHERE playlist_id=? AND source_message_id IN (12,13)",(pid,))
        self.db.execute("UPDATE playlist_items SET status='duplicate_in_playlist' WHERE playlist_id=? AND source_message_id=14",(pid,))
        self.db.commit()
        result=playlist_delivery_summary(self.db,pid)
        self.assertEqual(result,{"existing":1,"playlist_duplicates":1,"new":2,"forwarded":0,"dry_run":2,"missing":0,"errors":0})

    def test_dedup(self):
        self.db.execute("INSERT INTO destination_media VALUES(?,?,?,?)",(456,"doc-a",88,"now"))
        self.db.commit()
        self.assertTrue(destination_has_media(self.db,456,"doc-a"))
        self.assertFalse(destination_has_media(self.db,456,"doc-b"))

if __name__=="__main__":
    unittest.main()


class FakeAudioAttribute:
    def __init__(self, title=None, performer=None):
        self.title = title
        self.performer = performer


class FakeDocument:
    def __init__(self, doc_id, mime_type="audio/mpeg", attributes=None):
        self.id = doc_id
        self.mime_type = mime_type
        self.attributes = attributes or []


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
            "performer": "Artist", "mime_type": "audio/mpeg",
        })

    def test_duplicate_groups_separates_exact_and_metadata_matches(self):
        records = [
            {"document_id": "a", "message_id": 1, "title": "Song", "performer": "Artist"},
            {"document_id": "a", "message_id": 2, "title": "Song", "performer": "Artist"},
            {"document_id": "b", "message_id": 3, "title": " song ", "performer": "ARTIST"},
            {"document_id": "c", "message_id": 4, "title": "", "performer": "Artist"},
        ]
        exact, metadata = duplicate_groups(records)
        self.assertEqual([group["document_id"] for group in exact], ["a"])
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0]["title"], "song")
        self.assertEqual(len(metadata[0]["records"]), 3)

if __name__ == "__main__":
    unittest.main()
