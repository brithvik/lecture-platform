# One image for both the API and the worker; docker-compose picks which command runs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HF_HOME=/data/hf-cache

WORKDIR /app

# Install libraries first so Docker can reuse this layer when only your code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py worker.py db.py jobqueue.py ./
COPY web ./web

# Run as a regular user, not root (a basic container security practice)
RUN useradd --create-home appuser && mkdir -p /data && chown appuser /data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]