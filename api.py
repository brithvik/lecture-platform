"""
The API: accepts uploads and serves lecture info, transcripts, video, and the web pages.
Run:  uvicorn api:app --reload
"""
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import db
import jobqueue
import storage

ALLOWED = {".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav"}
PUBLIC_FIELDS = "id, title, status, attempts, duration_sec, processing_sec, error"
CHUNK = 256 * 1024

app = FastAPI(title="Lecture API")
db.init()
storage.ensure_bucket()


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
    key = f"videos/{lecture_id}{ext}"
    storage.upload(file.file, key, file.content_type or "application/octet-stream")

    with db.db() as c:
        c.execute(
            "INSERT INTO lectures (id, title, video_key) VALUES (%s, %s, %s)",
            (lecture_id, Path(file.filename).stem, key),
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
def lecture_video(lecture_id: str, request: Request):
    """Stream the video from object storage. Supports range requests, which browsers use to
    jump to the middle of a video without downloading all of it."""
    with db.db() as c:
        row = get_lecture(c, lecture_id)
    key = row.get("video_key")
    if not key:
        raise HTTPException(404, "This lecture's video was uploaded before the storage upgrade")

    info = storage.head(key)
    size = info["ContentLength"]
    media_type = info.get("ContentType") or "video/mp4"
    headers = {"Accept-Ranges": "bytes"}

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", "").strip())
    if not match or match.groups() == ("", ""):
        obj = storage.get(key)
        headers["Content-Length"] = str(size)
        return StreamingResponse(obj["Body"].iter_chunks(CHUNK), media_type=media_type, headers=headers)

    first, last = match.groups()
    if first:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    else:  # "bytes=-500" means the last 500 bytes
        start, end = max(size - int(last), 0), size - 1
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    obj = storage.get(key, f"bytes={start}-{end}")
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(obj["Body"].iter_chunks(CHUNK), status_code=206,
                             media_type=media_type, headers=headers)


# Serve the web pages (library and player). Must stay at the bottom, after all API routes.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
