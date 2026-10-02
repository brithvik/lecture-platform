"""
The API: accepts uploads and serves lecture info, transcripts, and video.
Run:  uvicorn api:app --reload
Then open http://localhost:8000 for the app, or http://localhost:8000/docs to test the API.
"""
import shutil
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db

STORAGE = db.DATA_DIR / "storage"
STORAGE.mkdir(exist_ok=True)
ALLOWED = {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav"}

app = FastAPI(title="Lecture API")
db.init()


def get_lecture(c, lecture_id):
    row = c.execute("SELECT * FROM lectures WHERE id = ?", (lecture_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Lecture not found")
    return row


@app.post("/lectures", status_code=201)
def upload_lecture(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Use one of: {', '.join(sorted(ALLOWED))}")

    lecture_id = uuid.uuid4().hex[:12]
    path = STORAGE / f"{lecture_id}{ext}"
    with open(path, "wb") as out:
        shutil.copyfileobj(file.file, out)  # streams to disk instead of loading it all into memory

    with db.db() as c:
        c.execute(
            "INSERT INTO lectures (id, title, video_path, created_at) VALUES (?, ?, ?, ?)",
            (lecture_id, Path(file.filename).stem, str(path), time.time()),
        )
    return {"id": lecture_id, "status": "queued"}


@app.get("/lectures")
def list_lectures():
    with db.db() as c:
        rows = c.execute(
            "SELECT id, title, status, duration_sec, processing_sec, error "
            "FROM lectures ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/lectures/{lecture_id}")
def lecture_status(lecture_id: str):
    with db.db() as c:
        row = get_lecture(c, lecture_id)
    return {k: row[k] for k in ("id", "title", "status", "duration_sec", "processing_sec", "error")}


@app.get("/lectures/{lecture_id}/segments")
def lecture_segments(lecture_id: str):
    with db.db() as c:
        row = get_lecture(c, lecture_id)
        if row["status"] != "done":
            raise HTTPException(409, f"Transcript isn't ready yet (status: {row['status']})")
        segs = c.execute(
            "SELECT start_sec, end_sec, text FROM segments WHERE lecture_id = ? ORDER BY idx",
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