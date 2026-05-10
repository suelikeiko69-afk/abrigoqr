# Dockerfile alternativo (caso prefira Fly.io, Railway ou self-host).
# Render usa render.yaml por padrao, nao precisa deste arquivo.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-cloud.txt ./
RUN pip install -r requirements-cloud.txt

COPY app.py ./
COPY static ./static

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
