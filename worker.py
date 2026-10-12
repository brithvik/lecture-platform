"""
The worker: takes jobs from the Redis queue and transcribes them.

Crash safety, in three layers:
  1. Graceful handoff: when Kubernetes stops this pod (SIGTERM), the current job is put back in the queue.
  2. Heartbeats + leases: while working, we update heartbeat_at every few seconds. If a worker dies
     without warning, its heartbeat goes stale, and any worker requeues the job after LEASE_SECONDS.
  3. Retry limit: a job that keeps killing workers is marked failed after MAX_ATTEMPTS.
PostgreSQL is the source of truth; Redis only delivers job IDs, so duplicates are harmless.
"""
import os
import signal
import socket
import threading
import time

from faster_whisper import WhisperModel

import db
import jobqueue

MODEL_SIZE = os.environ.get("MODEL_SIZE", "base")
CPU_THREADS = int(os.environ.get("CPU_THREADS", os.cpu_count() or 4))
WORKER_ID = os.environ.get("HOSTNAME", socket.gethostname())  # the pod name in Kubernetes
HEARTBEAT_SECONDS = 10
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", 60))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", 3))
STALE_QUEUED_SECONDS = 120   # re-send queued jobs nobody picked up (e.g. Redis restarted)
RECOVER_EVERY_SECONDS = 15

current_job = {"id": None}


def log(msg):
    print(f"[{WORKER_ID}] {msg}", flush=True)


def claim(lecture_id):
    """Take ownership of a job, but only if it's still queued. If two workers get the same ID,
    exactly one wins, because the database checks and updates the row in one step."""
    with db.db() as c:
        return c.execute(
            """UPDATE lectures
               SET status = 'processing', worker_id = %s, heartbeat_at = now(), attempts = attempts + 1
               WHERE id = %s AND status = 'queued'
               RETURNING id, title, video_path, attempts""",
            (WORKER_ID, lecture_id),
        ).fetchone()


def heartbeat(lecture_id, stop):
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            with db.db() as c:
                c.execute("UPDATE lectures SET heartbeat_at = now() WHERE id = %s AND worker_id = %s",
                          (lecture_id, WORKER_ID))
        except Exception as e:
            log(f"Heartbeat failed: {e}")


def recover_stale_jobs():
    """Requeue jobs whose worker died. Safe for every worker to run at the same time."""
    stale = "heartbeat_at < now() - %s * interval '1 second'"
    with db.db() as c:
        gave_up = c.execute(
            f"""UPDATE lectures SET status = 'failed', worker_id = NULL,
                    error = 'Gave up after ' || attempts || ' attempts (worker kept dying)'
                WHERE status = 'processing' AND {stale} AND attempts >= %s RETURNING id""",
            (LEASE_SECONDS, MAX_ATTEMPTS),
        ).fetchall()
        requeued = c.execute(
            f"""UPDATE lectures SET status = 'queued', queued_at = now(), worker_id = NULL
                WHERE status = 'processing' AND {stale} RETURNING id""",
            (LEASE_SECONDS,),
        ).fetchall()
        waiting = c.execute(
            """SELECT id FROM lectures
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
                    """UPDATE lectures SET status = 'queued', queued_at = now(), worker_id = NULL,
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


def process(model, job):
    start = time.time()
    segments, info = model.transcribe(job["video_path"], vad_filter=True)
    rows = [(job["id"], i, round(s.start, 2), round(s.end, 2), s.text.strip())
            for i, s in enumerate(segments)]
    elapsed = time.time() - start

    with db.db() as c:
        c.execute("DELETE FROM segments WHERE lecture_id = %s", (job["id"],))
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO segments (lecture_id, idx, start_sec, end_sec, text) VALUES (%s, %s, %s, %s, %s)",
                rows,
            )
        done = c.execute(
            """UPDATE lectures SET status = 'done', duration_sec = %s, processing_sec = %s,
                   error = NULL, worker_id = NULL
               WHERE id = %s AND worker_id = %s RETURNING id""",
            (info.duration, elapsed, job["id"], WORKER_ID),
        ).fetchone()
    if not done:
        log(f"Finished {job['id']}, but another worker had taken it over; discarding my result")
        return
    log(f"Done: {job['title']} | {len(rows)} segments | {info.duration / 60:.1f} min of audio "
        f"in {elapsed / 60:.1f} min ({info.duration / elapsed:.1f}x real time)")


def handle_failure(job, error):
    retry = job["attempts"] < MAX_ATTEMPTS
    with db.db() as c:
        c.execute(
            """UPDATE lectures SET status = %s, error = %s, worker_id = NULL, queued_at = now()
               WHERE id = %s AND worker_id = %s""",
            ("queued" if retry else "failed", str(error), job["id"], WORKER_ID),
        )
    if retry:
        jobqueue.enqueue(job["id"])
        log(f"Attempt {job['attempts']} failed for {job['id']} ({error}); retrying")
    else:
        log(f"Giving up on {job['id']} after {job['attempts']} attempts: {error}")


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

        log(f"Transcribing: {job['title']} ({job['id']}), attempt {job['attempts']}")
        current_job["id"] = job["id"]
        stop = threading.Event()
        threading.Thread(target=heartbeat, args=(job["id"], stop), daemon=True).start()
        try:
            process(model, job)
        except Exception as e:
            handle_failure(job, e)
        finally:
            stop.set()
            current_job["id"] = None


if __name__ == "__main__":
    main()