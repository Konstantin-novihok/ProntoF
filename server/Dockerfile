FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY server/app.py ./app.py
COPY server/rendering.py ./rendering.py

ENV DATA_DIR=/data \
    WHISPER_MODEL=small \
    CPU_THREADS=2 \
    MAX_UPLOAD_GB=500 \
    INTERNAL_ONLY=1 \
    PIPER_VOICE_DIR=/data/piper-voices

VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
