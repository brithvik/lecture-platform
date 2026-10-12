"""
The worker: takes jobs from the Redis queue. Two kinds of jobs:
  split: download a lecture, cut its audio into chunks at quiet moments, queue one job per chunk
  chunk: transcribe one chunk; whoever finishes the last chunk marks the lecture done
Many workers can transcribe chunks of the same lecture at the same time.

Crash safety, per job:
  1. Graceful handoff: on SIGTERM (Kubernetes stopping the pod), the current job goes back in the queue.
  2. Heartbeats + leases: if a worker dies without warning, its job is requeued after LEASE_SECONDS.
  3. Retry limit: a job that keeps failing is marked failed after MAX_ATTEMPTS (and so is its lecture).
PostgreSQL is the source of truth; Redis only delivers job IDs, so duplicates are harmless.
"""
import os
import signal
import socket
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel, decode_audio

import db
import jobqueue
import storage

MODEL_SIZE = os.environ.get("MODEL_SIZE", "base")
CPU_THREADS = int(os.environ.get("CPU_THREADS", os.cpu_count() or 4))
WORKER_ID = os.environ.get("HOSTNAME", socket.gethostname())  # the pod name in Kubernetes
CHUNK_SECONDS = int(os.environ.get("CHUNK_SECONDS", 120))
CUT_SEARCH_SECONDS = 10      # look this far either side of each cut point for a quiet moment
SAMPLE_RATE = 16000          # Whisper works on 16 kHz mono audio
SEGMENT_STRIDE = 100000      # segment idx = chunk number * stride + position, so ORDER BY idx works
HEARTBEAT_SECONDS = 10
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", 60))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", 3))
STALE_QUEUED_SECONDS = 120   # re-send queued jobs nobody picked up (e.g. Redis restarted)
RECOVER_EVERY_SECONDS = 15

current_job = {"id": None}


def log(msg):
    print(f"[{WORKER_ID}] {msg}", flush=True)


# ---------- job ownership: claim, heartbeat, recovery, handoff ----------

def claim(job_id):
    """Take ownership of a job, but only if it's still queued. If two workers get the same ID,
    exactly one wins, because the database checks and updates the row in one step."""
    with db.db() as c:
        job = c.execute(
            """UPDATE jobs
               SET status = 'processing', worker_id = %s, heartbeat_at = now(), attempts = attempts + 1
               WHERE id = %s AND status = 'queued'
               RETURNING *""",
            (WORKER_ID, job_id),
        ).fetchone()
        if job and job["kind"] == "split":
            job.update(c.execute(
                """UPDATE lectures SET status = 'processing', started_at = COALESCE(started_at, now())
                   WHERE id = %s RETURNING title, video_key""",
                (job["lecture_id"],),
            ).fetchone() or {})
        elif job:
            job.update(c.execute("SELECT title, total_chunks FROM lectures WHERE id = %s",
                                 (job["lecture_id"],)).fetchone() or {})
        return job


def heartbeat(job_id, stop):
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            with db.db() as c:
                c.execute("UPDATE jobs SET heartbeat_at = now() WHERE id = %s AND worker_id = %s",
                          (job_id, WORKER_ID))
        except Exception as e:
            log(f"Heartbeat failed: {e}")


def fail_lectures(c, rows, reason):
    for r in rows:
        c.execute("UPDATE lectures SET status = 'failed', error = %s WHERE id = %s",
                  (f"{reason} (job {r['id']})", r["lecture_id"]))


def recover_stale_jobs():
    """Requeue jobs whose worker died. Safe for every worker to run at the same time."""
    stale = "heartbeat_at < now() - %s * interval '1 second'"
    with db.db() as c:
        gave_up = c.execute(
            f"""UPDATE jobs SET status = 'failed', worker_id = NULL,
                    error = 'Gave up after ' || attempts || ' attempts (worker kept dying)'
                WHERE status = 'processing' AND {stale} AND attempts >= %s RETURNING id, lecture_id""",
            (LEASE_SECONDS, MAX_ATTEMPTS),
        ).fetchall()
        fail_lectures(c, gave_up, "A job kept crashing workers")
        requeued = c.execute(
            f"""UPDATE jobs SET status = 'queued', queued_at = now(), worker_id = NULL
                WHERE status = 'processing' AND {stale} RETURNING id""",
            (LEASE_SECONDS,),
        ).fetchall()
        waiting = c.execute(
            """SELECT id FROM jobs
               WHERE status = 'queued' AND queued_at < now() - %s * interval '1 second'""",
            (STALE_QUEUED_SECONDS,),
        ).fetchall()

    for r in requeued:
        jobqueue.enqueue(r["id"])
        log(f"Recovered {r['id']} from a dead worker; back in the queue")
    for r in waiting:
        if not jobqueue.is_waiting(r["id"]):
            jobqueue.enqueue(r["id"])
            log(f"Re-sent {r['id']}, which was queued but missing from Redis")
    for r in gave_up:
        log(f"Marked {r['id']} as failed after {MAX_ATTEMPTS} attempts")


def release_current_job(signum, frame):
    """Kubernetes sends SIGTERM before stopping a pod: hand the job back instead of stranding it."""
    job_id = current_job["id"]
    if job_id:
        try:
            with db.db() as c:
                c.execute(
                    """UPDATE jobs SET status = 'queued', queued_at = now(), worker_id = NULL,
                           attempts = GREATEST(attempts - 1, 0)   -- a shutdown isn't the job's fault
                       WHERE id = %s AND worker_id = %s""",
                    (job_id, WORKER_ID),
                )
            jobqueue.enqueue(job_id)
            log(f"Shutting down: handed {job_id} back to the queue")
        except Exception as e:
            log(f"Shutting down: couldn't hand back {job_id} ({e}); its lease will expire instead")
    else:
        log("Shutting down: no job in progress")
    os._exit(0)


def handle_failure(job, error):
    retry = job["attempts"] < MAX_ATTEMPTS
    with db.db() as c:
        c.execute(
            """UPDATE jobs SET status = %s, error = %s, worker_id = NULL, queued_at = now()
               WHERE id = %s AND worker_id = %s""",
            ("queued" if retry else "failed", str(error), job["id"], WORKER_ID),
        )
        if not retry:
            fail_lectures(c, [job], f"Failed after {job['attempts']} attempts: {error}")
    if retry:
        jobqueue.enqueue(job["id"])
        log(f"Attempt {job['attempts']} failed for {job['id']} ({error}); retrying")
    else:
        log(f"Giving up on {job['id']} after {job['attempts']} attempts: {error}")


# ---------- split: cut a lecture into chunks at quiet moments ----------

def choose_cuts(audio):
    """Return (start, end) sample ranges about CHUNK_SECONDS long, cutting at the quietest
    quarter-second near each target so words aren't sliced in half."""
    n = len(audio)
    target, window, frame = CHUNK_SECONDS * SAMPLE_RATE, CUT_SEARCH_SECONDS * SAMPLE_RATE, SAMPLE_RATE // 4
    cuts, pos = [0], 0
    while n - pos > target + window:
        lo = pos + target - window
        region = audio[lo:lo + 2 * window]
        frames = len(region) // frame
        loudness = (region[:frames * frame].reshape(frames, frame) ** 2).mean(axis=1)
        pos = lo + int(loudness.argmin()) * frame + frame // 2
        cuts.append(pos)
    cuts.append(n)
    return list(zip(cuts[:-1], cuts[1:]))


def write_wav(path, samples):
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def process_split(job):
    if not job.get("video_key"):
        raise RuntimeError("No video in storage (uploaded before the storage upgrade)")
    start = time.time()
    lecture_id = job["lecture_id"]
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / Path(job["video_key"]).name
        storage.download(job["video_key"], video)
        audio = decode_audio(str(video), sampling_rate=SAMPLE_RATE)
        ranges = choose_cuts(audio)
        chunks = []
        for i, (a, b) in enumerate(ranges):
            key = f"audio/{lecture_id}/{i:04d}.wav"
            wav = Path(tmp) / f"{i:04d}.wav"
            write_wav(wav, audio[a:b])
            with open(wav, "rb") as f:
                storage.upload(f, key, "audio/wav")
            chunks.append((f"{lecture_id}:chunk:{i}", lecture_id, i, a / SAMPLE_RATE, b / SAMPLE_RATE, key))
    duration = len(audio) / SAMPLE_RATE

    with db.db() as c:
        if not c.execute("""UPDATE jobs SET status = 'done', worker_id = NULL, processing_sec = %s
                            WHERE id = %s AND worker_id = %s AND status = 'processing' RETURNING id""",
                         (time.time() - start, job["id"], WORKER_ID)).fetchone():
            log(f"Finished {job['id']}, but another worker had taken it over; discarding my result")
            return
        # Start clean, so re-running a split never leaves duplicate chunks or segments behind
        c.execute("DELETE FROM jobs WHERE lecture_id = %s AND kind = 'chunk'", (lecture_id,))
        c.execute("DELETE FROM segments WHERE lecture_id = %s", (lecture_id,))
        with c.cursor() as cur:
            cur.executemany(
                """INSERT INTO jobs (id, lecture_id, kind, chunk_idx, start_sec, end_sec, audio_key)
                   VALUES (%s, %s, 'chunk', %s, %s, %s, %s)""",
                chunks,
            )
        c.execute("UPDATE lectures SET total_chunks = %s, chunks_done = 0, duration_sec = %s WHERE id = %s",
                  (len(chunks), duration, lecture_id))
    for chunk in chunks:
        jobqueue.enqueue(chunk[0])
    log(f"Split {job['title']} ({duration / 60:.1f} min) into {len(chunks)} chunks "
        f"in {time.time() - start:.1f}s")


# ---------- chunk: transcribe one piece ----------

def process_chunk(model, job):
    start = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "chunk.wav"
        storage.download(job["audio_key"], path)
        segments, _ = model.transcribe(str(path), vad_filter=True)
        base = job["chunk_idx"] * SEGMENT_STRIDE
        offset = job["start_sec"]
        rows = [(job["lecture_id"], base + i, round(offset + s.start, 2), round(offset + s.end, 2), s.text.strip())
                for i, s in enumerate(segments)]
    elapsed = time.time() - start

    with db.db() as c:
        if not c.execute("""UPDATE jobs SET status = 'done', worker_id = NULL, processing_sec = %s, error = NULL
                            WHERE id = %s AND worker_id = %s AND status = 'processing' RETURNING id""",
                         (elapsed, job["id"], WORKER_ID)).fetchone():
            log(f"Finished {job['id']}, but another worker had taken it over; discarding my result")
            return
        c.execute("DELETE FROM segments WHERE lecture_id = %s AND idx >= %s AND idx < %s",
                  (job["lecture_id"], base, base + SEGMENT_STRIDE))
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO segments (lecture_id, idx, start_sec, end_sec, text) VALUES (%s, %s, %s, %s, %s)",
                rows,
            )
        lecture = c.execute(
            """UPDATE lectures SET chunks_done = chunks_done + 1 WHERE id = %s
               RETURNING chunks_done, total_chunks, duration_sec""",
            (job["lecture_id"],),
        ).fetchone()
        finished = lecture["chunks_done"] >= lecture["total_chunks"]
        if finished:
            total = c.execute(
                """UPDATE lectures SET status = 'done', error = NULL,
                       processing_sec = EXTRACT(EPOCH FROM now() - started_at)
                   WHERE id = %s RETURNING processing_sec""",
                (job["lecture_id"],),
            ).fetchone()["processing_sec"]

    log(f"Chunk {job['chunk_idx'] + 1}/{lecture['total_chunks']} of {job['title']}: "
        f"{len(rows)} segments in {elapsed:.1f}s")
    if finished:
        log(f"Done: {job['title']} | {lecture['duration_sec'] / 60:.1f} min of audio in {total / 60:.1f} min "
            f"({lecture['duration_sec'] / total:.1f}x real time, upload to finish)")


# ---------- main loop ----------

def main():
    db.init()
    signal.signal(signal.SIGTERM, release_current_job)
    log(f"Loading model '{MODEL_SIZE}' using {CPU_THREADS} CPU threads...")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)
    log("Worker ready. Waiting for jobs...")

    last_recovery = 0.0
    while True:
        try:
            if time.time() - last_recovery > RECOVER_EVERY_SECONDS:
                recover_stale_jobs()
                last_recovery = time.time()

            job_id = jobqueue.wait_for_job(timeout=5)
            if not job_id:
                continue
            job = claim(job_id)
            if not job:
                continue  # already taken or finished; duplicates in the queue are harmless
        except Exception as e:
            log(f"Queue or database unavailable ({e}); retrying in 3s")
            time.sleep(3)
            continue

        current_job["id"] = job["id"]
        stop = threading.Event()
        threading.Thread(target=heartbeat, args=(job["id"], stop), daemon=True).start()
        try:
            if job["kind"] == "split":
                log(f"Splitting: {job['title']} ({job['lecture_id']}), attempt {job['attempts']}")
                process_split(job)
            else:
                process_chunk(model, job)
        except Exception as e:
            handle_failure(job, e)
        finally:
            stop.set()
            current_job["id"] = None


if __name__ == "__main__":
    main()
