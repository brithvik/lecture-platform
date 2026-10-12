"""
The API: accepts uploads and serves lecture info, transcripts, video, and the web pages.
Run:  uvicorn api:app --reload
"""
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db
import jobqueue

STORAGE = db.DATA_DIR / "storage"
STORAGE.mkdir(exist_ok=True)
ALLOWED = {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav"}
PUBLIC_FIELDS = "id, title, status, attempts, duration_sec, processing_sec, error"

app = FastAPI(title="Lecture API")
db.init()


def get_lecture(c, lecture_id):
    row = c.execute("SELECT * FROM lectures WHERE id = %s", (lecture_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Lecture not found")
    return row


@app.get("/healthz")
def healthz():
    """Liveness: the process is up. Deliberately doesn't touch the database."""
    return {"ok": True}


@app.get("/readyz")
def readyz():
    """Readiness: only accept traffic when the database is reachable."""
    try:
        with db.db() as c:
            c.execute("SELECT 1")
    except Exception:
        raise HTTPException(503, "Database unavailable")
    return {"ok": True}


@app.post("/lectures", status_code=201)
def upload_lecture(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Use one of: {', '.join(sorted(ALLOWED))}")

    lecture_id = uuid.uuid4().hex[:12]
    path = STORAGE / f"{lecture_id}{ext}"
    with open(path, "wb") as out:
        shutil.copyfileobj(file.file, out)

    with db.db() as c:
        c.execute(
            "INSERT INTO lectures (id, title, video_path) VALUES (%s, %s, %s)",
            (lecture_id, Path(file.filename).stem, str(path)),
        )
    try:
        jobqueue.enqueue(lecture_id)
    except Exception as e:
        # Not fatal: workers re-send jobs that sit queued too long, so this still gets processed
        print(f"Could not notify queue for {lecture_id}: {e}")
    return {"id": lecture_id, "status": "queued"}


@app.get("/lectures")
def list_lectures():
    with db.db() as c:
        return c.execute(f"SELECT {PUBLIC_FIELDS} FROM lectures ORDER BY created_at DESC").fetchall()


@app.get("/lectures/{lecture_id}")
def lecture_status(lecture_id: str):
    with db.db() as c:
        row = get_lecture(c, lecture_id)
    return {k: row[k] for k in PUBLIC_FIELDS.split(", ")}


@app.get("/lectures/{lecture_id}/segments")
def lecture_segments(lecture_id: str):
    with db.db() as c:
        row = get_lecture(c, lecture_id)
        if row["status"] != "done":
            raise HTTPException(409, f"Transcript isn't ready yet (status: {row['status']})")
        segs = c.execute(
            "SELECT start_sec, end_sec, text FROM segments WHERE lecture_id = %s ORDER BY idx",
            (lecture_id,),
        ).fetchall()
    return [{"start": s["start_sec"], "end": s["end_sec"], "text": s["text"]} for s in segs]


@app.get("/lectures/{lecture_id}/video")
def lecture_video(lecture_id: str):
    with db.db() as c:
        row = get_lecture(c, lecture_id)
    return FileResponse(row["video_path"])


# Serve the web pages (library and player). Must stay at the bottom, after all API routes.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")