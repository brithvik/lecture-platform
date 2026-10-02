"""
The worker: picks up queued lectures and transcribes them in the background.
Run:  python worker.py   (in its own terminal, alongside the API)
"""
import os
import time

from faster_whisper import WhisperModel

import db

# Settings come from environment variables (set in docker-compose.yml), with defaults
MODEL_SIZE = os.environ.get("MODEL_SIZE", "base")
CPU_THREADS = int(os.environ.get("CPU_THREADS", os.cpu_count() or 4))
POLL_SECONDS = 3


def claim_next():
    """Grab the oldest queued lecture and mark it 'processing' in one step,
    so two workers can never grab the same lecture."""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")  # locks the database for this short moment
        row = conn.execute(
            "SELECT id, title, video_path FROM lectures "
            "WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            conn.execute("UPDATE lectures SET status = 'processing' WHERE id = ?", (row["id"],))
        conn.commit()
        return row
    finally:
        conn.close()


def process(model, row):
    start = time.time()
    segments, info = model.transcribe(row["video_path"], vad_filter=True)
    rows = [(row["id"], i, round(s.start, 2), round(s.end, 2), s.text.strip())
            for i, s in enumerate(segments)]
    elapsed = time.time() - start

    with db.db() as c:
        c.execute("DELETE FROM segments WHERE lecture_id = ?", (row["id"],))  # safe if re-run
        c.executemany(
            "INSERT INTO segments (lecture_id, idx, start_sec, end_sec, text) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        c.execute(
            "UPDATE lectures SET status = 'done', duration_sec = ?, processing_sec = ?, error = NULL "
            "WHERE id = ?",
            (info.duration, elapsed, row["id"]),
        )
    print(f"Done: {row['title']} | {len(rows)} segments | {info.duration / 60:.1f} min of audio "
          f"in {elapsed / 60:.1f} min ({info.duration / elapsed:.1f}x real time)")


def main():
    db.init()
    print(f"Loading model '{MODEL_SIZE}' using {CPU_THREADS} CPU threads...")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)
    print("Worker ready. Waiting for lectures...")

    while True:
        row = claim_next()
        if not row:
            time.sleep(POLL_SECONDS)
            continue
        print(f"Transcribing: {row['title']} ({row['id']})")
        try:
            process(model, row)
        except Exception as e:
            with db.db() as c:
                c.execute("UPDATE lectures SET status = 'failed', error = ? WHERE id = ?", (str(e), row["id"]))
            print(f"Failed: {row['title']}: {e}")


if __name__ == "__main__":
    main()