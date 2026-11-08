"""Regression tests for the admin user-list endpoint."""
import asyncio
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from mcat import web


class AdminUserListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = sqlite3.connect(Path(self.tmp.name) / "auth.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE web_users (
                username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                role TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE web_sessions (
                token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        self.token = "test-session-token"
        self.csrf = "test-csrf-token"
        now = datetime.now(timezone.utc)
        self.db.execute(
            "INSERT INTO web_users VALUES(?,?,?,?,?)",
            ("admin", "unused", "admin", 1, now.isoformat()),
        )
        self.db.execute(
            "INSERT INTO web_users VALUES(?,?,?,?,?)",
            ("existing.user", "unused", "user", 1, now.isoformat()),
        )
        self.db.execute(
            "INSERT INTO web_sessions VALUES(?,?,?,?,?)",
            (web._token_hash(self.token), "admin", self.csrf,
             (now + timedelta(hours=1)).isoformat(), now.isoformat()),
        )
        self.db.commit()
        self.app = SimpleNamespace(state=SimpleNamespace(auth_db=self.db))

    def request(self, *, authenticated=True):
        headers = []
        if authenticated:
            headers.append((b"cookie", f"{web.SESSION_COOKIE}={self.token}".encode()))
        scope = {
            "type": "http", "asgi": {"version": "3.0"},
            "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": "/api/admin/users",
            "raw_path": b"/api/admin/users", "query_string": b"",
            "headers": headers, "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80), "app": self.app,
        }
        return Request(scope)

    def test_admin_list_returns_existing_users(self):
        result = asyncio.run(web.list_users(self.request()))
        self.assertEqual(
            [user["username"] for user in result["users"]],
            ["admin", "existing.user"],
        )

    def test_unauthenticated_list_is_rejected(self):
        with self.assertRaises(web.HTTPException) as raised:
            asyncio.run(web.list_users(self.request(authenticated=False)))
        self.assertEqual(raised.exception.status_code, 401)

    def test_non_admin_list_is_rejected(self):
        self.db.execute("UPDATE web_users SET role='user' WHERE username='admin'")
        self.db.commit()
        with self.assertRaises(web.HTTPException) as raised:
            asyncio.run(web.list_users(self.request()))
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
