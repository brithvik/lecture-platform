"""Redis job queue. Redis only *delivers* job IDs quickly; PostgreSQL is the source of truth.
(Named jobqueue.py because Python already has a built-in module called 'queue'.)"""
import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
QUEUE = "lecture:jobs"

_client = None


def client():
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def enqueue(lecture_id):
    client().lpush(QUEUE, lecture_id)


def is_waiting(lecture_id):
    return client().lpos(QUEUE, lecture_id) is not None


def wait_for_job(timeout=5):
    """Block up to `timeout` seconds for a job ID. Returns None if nothing arrived."""
    result = client().brpop(QUEUE, timeout=timeout)
    return result[1] if result else None
