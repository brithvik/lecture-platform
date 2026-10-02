"""Shared database code used by both the API and the worker."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Where data lives. Locally: this folder. In Docker: a shared volume (DATA_DIR=/data).
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "lectures.db"


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row  # rows behave like dictionaries
    return conn


@contextmanager
def db():
    """Open a connection, commit if everything worked, always close."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS lectures (
            id             TEXT PRIMARY KEY,
            title          TEXT NOT NULL,
            video_path     TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'queued',  -- queued, processing, done, failed
            created_at     REAL NOT NULL,
            duration_sec   REAL,
            processing_sec REAL,
            error          TEXT
        );
        CREATE TABLE IF NOT EXISTS segments (
            lecture_id TEXT NOT NULL REFERENCES lectures(id),
            idx        INTEGER NOT NULL,
            start_sec  REAL NOT NULL,
            end_sec    REAL NOT NULL,
            text       TEXT NOT NULL,
            PRIMARY KEY (lecture_id, idx)
        );
        """)