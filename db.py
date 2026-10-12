"""Shared database code (PostgreSQL) used by both the API and the worker."""
import os
import time
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://lecture:lecture@localhost:5432/lecture")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lectures (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    video_key      TEXT,                              -- where the video lives in object storage
    status         TEXT NOT NULL DEFAULT 'queued',   -- queued, processing, done, failed
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,                       -- when processing began
    total_chunks   INTEGER,                           -- set once the audio is split
    chunks_done    INTEGER NOT NULL DEFAULT 0,
    duration_sec   DOUBLE PRECISION,
    processing_sec DOUBLE PRECISION,                  -- upload-to-done wall clock time
    error          TEXT
);

-- Upgrades for tables created by earlier versions
ALTER TABLE lectures ADD COLUMN IF NOT EXISTS video_key TEXT;
ALTER TABLE lectures ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE lectures ADD COLUMN IF NOT EXISTS total_chunks INTEGER;
ALTER TABLE lectures ADD COLUMN IF NOT EXISTS chunks_done INTEGER NOT NULL DEFAULT 0;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'lectures' AND column_name = 'video_path') THEN
        ALTER TABLE lectures ALTER COLUMN video_path DROP NOT NULL;
    END IF;
END $$;

-- Every unit of work: one "split" job per lecture, then one "chunk" job per piece of audio
CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,                  -- e.g. "abc123:split" or "abc123:chunk:7"
    lecture_id     TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,                     -- split or chunk
    chunk_idx      INTEGER,
    start_sec      DOUBLE PRECISION,                  -- where this chunk sits in the lecture
    end_sec        DOUBLE PRECISION,
    audio_key      TEXT,                              -- the chunk's audio in object storage
    status         TEXT NOT NULL DEFAULT 'queued',   -- queued, processing, done, failed
    attempts       INTEGER NOT NULL DEFAULT 0,
    worker_id      TEXT,
    heartbeat_at   TIMESTAMPTZ,
    queued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processing_sec DOUBLE PRECISION,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);

CREATE TABLE IF NOT EXISTS segments (
    lecture_id TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,                      -- chunk number * 100000 + position in chunk
    start_sec  DOUBLE PRECISION NOT NULL,
    end_sec    DOUBLE PRECISION NOT NULL,
    text       TEXT NOT NULL,
    PRIMARY KEY (lecture_id, idx)
);
"""


@contextmanager
def db():
    """Open a connection; commits if everything worked, rolls back on error, always closes."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


def init(retries=30):
    """Create or update tables, waiting for the database to come up (it may start after us)."""
    for attempt in range(1, retries + 1):
        try:
            with db() as c:
                c.execute("SELECT pg_advisory_xact_lock(4242)")  # only one pod migrates at a time
                c.execute(SCHEMA)
            return
        except psycopg.OperationalError as e:
            print(f"Database not ready ({attempt}/{retries}): {e}".strip())
            time.sleep(2)
    raise RuntimeError("Could not connect to the database")
