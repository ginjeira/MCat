import sqlite3
import unittest
from mcat.playlist_engine import add_media, active_playlist, destination_has_media, finish_playlist, init_playlist_schema, is_finished, is_playlist_start, playlist_delivery_summary, start_playlist

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
        self.db.execute("UPDATE playlist_items SET status='duplicate' WHERE playlist_id=? AND source_message_id=11",(pid,))
        self.db.execute("UPDATE playlist_items SET status='dry_run' WHERE playlist_id=? AND source_message_id IN (12,13)",(pid,))
        self.db.commit()
        result=playlist_delivery_summary(self.db,pid)
        self.assertEqual(result,{"existing":1,"new":2,"forwarded":0,"dry_run":2,"missing":0,"errors":0})

    def test_dedup(self):
        self.db.execute("INSERT INTO destination_media VALUES(?,?,?,?)",(456,"doc-a",88,"now"))
        self.db.commit()
        self.assertTrue(destination_has_media(self.db,456,"doc-a"))
        self.assertFalse(destination_has_media(self.db,456,"doc-b"))

if __name__=="__main__":
    unittest.main()
