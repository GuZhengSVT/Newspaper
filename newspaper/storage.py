from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import tempfile
import time as clock
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import TZ, Settings


def utcnow() -> datetime:
    return datetime.now(TZ)


def expiry(day: str) -> float:
    # Rebuilds never extend retention. Keep the edition date and the next 29 dates.
    return datetime.combine(date.fromisoformat(day) + timedelta(days=30), time(), TZ).timestamp()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextlib.contextmanager
def lock(settings: Settings, wait_seconds: float = 0):
    settings.prepare()
    with (settings.data / "pipeline.lock").open("a") as stream:
        deadline = clock.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock.monotonic() >= deadline:
                    raise BlockingIOError("Another Newspaper job is already running")
                clock.sleep(0.05)
        yield


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.prepare()
        self.db = sqlite3.connect(settings.data / "newspaper.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA foreign_keys=ON;
          PRAGMA secure_delete=ON;
          PRAGMA journal_mode=DELETE;
          CREATE TABLE IF NOT EXISTS editions (
            day TEXT PRIMARY KEY, expires_at REAL NOT NULL, status TEXT NOT NULL,
            title TEXT NOT NULL, summary TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS events (
            id TEXT NOT NULL, day TEXT NOT NULL REFERENCES editions(day) ON DELETE CASCADE,
            description TEXT NOT NULL, PRIMARY KEY(id, day)
          );
          CREATE TABLE IF NOT EXISTS deliveries (
            day TEXT NOT NULL REFERENCES editions(day) ON DELETE CASCADE, channel TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY(day, channel)
          );
          CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, day TEXT NOT NULL, started_at REAL NOT NULL,
            finished_at REAL, status TEXT NOT NULL, error TEXT
          );
        """)

    def close(self) -> None:
        self.db.close()

    def editions(self, now: float | None = None) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM editions WHERE expires_at>? AND status IN ('ready','published') ORDER BY day DESC",
            (now if now is not None else utcnow().timestamp(),),
        )]

    def add(self, doc: dict, legacy: bool = False) -> None:
        self.db.execute(
            "INSERT INTO editions(day,expires_at,status,title,summary,legacy) VALUES(?,?,'ready',?,?,?)",
            (doc["date"], expiry(doc["date"]), doc["title"], doc["summary"], int(legacy)),
        )
        for section in doc.get("sections", []):
            for article in section["articles"]:
                self.db.execute("INSERT INTO events VALUES(?,?,?)", (article["event_key"], doc["date"], article["title"]))
        self.db.commit()

    def history(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT e.id,e.description,e.day FROM events e JOIN editions d ON e.day=d.day WHERE d.expires_at>? AND d.status='published'",
            (utcnow().timestamp(),),
        )]

    def document(self, day: str) -> dict:
        date.fromisoformat(day)
        return json.loads((self.settings.data / "editions" / day / "report.json").read_text())

    def published(self, day: str) -> bool:
        return self.db.execute("SELECT 1 FROM editions WHERE day=? AND status='published'", (day,)).fetchone() is not None
