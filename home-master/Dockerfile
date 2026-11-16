FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY home-master/server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY home-master/server/*.py ./
COPY home-master/web ./web

ENV DATA_DIR=/data \
    WEB_DIR=/srv/web \
    PYTHONPATH=/srv

VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
