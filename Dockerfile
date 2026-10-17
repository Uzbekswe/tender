# Always-on deploy (prepared, not deployed). Build context: the repo root. See render.yaml and README "Deploy".
FROM python:3.13-slim

# LibreOffice converts Office files to PDF (engine/ingest.py); unar opens RAR archives (rarfile).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
      unar fonts-dejavu-core fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PATH="/app/backend/.venv/bin:$PATH"

WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ ./

# Uploads, page images, the SQLite database and the LLM cache live in /data (DATA_DIR). Mount a persistent disk there.
EXPOSE 8010

# Render sets PORT; locally the default is 8010.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8010}"]
