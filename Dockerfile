FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf-cache

WORKDIR /app

# Install libraries first so Docker can reuse this layer when only your code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as a regular user, not root (a basic container security practice)
RUN useradd --create-home appuser

# Bake the Whisper model into the image: workers start fast and need no shared storage.
# (Only "base" is included; other sizes would need another line here.)
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')" \
    && chown -R appuser /opt/hf-cache
ENV HF_HUB_OFFLINE=1

COPY api.py worker.py db.py jobqueue.py storage.py ./
COPY web ./web

USER appuser
EXPOSE 8000
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
