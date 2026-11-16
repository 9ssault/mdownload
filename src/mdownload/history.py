"""Persistent job history in SQLite."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .config import HISTORY_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finished_at REAL NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    dest TEXT NOT NULL DEFAULT '',
    fmt TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_finished ON history(finished_at DESC);
"""


class History:
    def __init__(self, path=HISTORY_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # workers write from thread-pool threads
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    def add(self, title, url, dest, fmt, status, size=0, error="", finished_at=None):
        with self._lock:
            self.conn.execute(
                "INSERT INTO history (finished_at,title,url,dest,fmt,status,size,error)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (float(finished_at or time.time()), str(title)[:500], str(url)[:2000],
                 str(dest), str(fmt), str(status), int(size or 0), str(error or "")[:2000]),
            )
            self.conn.commit()

    def search(self, query: str = "", limit: int = 300) -> list[dict]:
        like = f"%{query.strip()}%"
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM history WHERE title LIKE ? OR url LIKE ? OR dest LIKE ?"
                " ORDER BY finished_at DESC LIMIT ?", (like, like, like, int(limit))
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, row_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM history WHERE id=?", (int(row_id),))
            self.conn.commit()

    def clear(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM history")
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
