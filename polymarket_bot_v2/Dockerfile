# =============================================================================
# Veyra.Trade V2 — user site / API container
#
# Builds the FastAPI dashboard (scripts.web_dashboard:app) as a single
# self-contained image. The private Admin V3 console is intentionally NOT
# included in this image.
#
#   docker build -t veyra-polymarket:latest ./polymarket_bot_v2
#   docker run --rm -p 8080:8080 --env-file polymarket_bot_v2/.env veyra-polymarket:latest
# =============================================================================

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# git        -> required by the git+https dependencies in requirements.txt
# gcc        -> required to compile the eccodes Python bindings
# libeccodes-dev -> ECMWF ecCodes C library for the weather data fetchers
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        gcc \
        libeccodes-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8080

# Gunicorn + UvicornWorker mirrors the production systemd unit. Operators can
# scale workers via the GUNICORN_WORKERS env var (default 2, matching a 2-core VPS).
CMD ["sh", "-c", "gunicorn scripts.web_dashboard:app \
    --bind 0.0.0.0:8080 \
    --workers ${GUNICORN_WORKERS:-2} \
    --worker-class uvicorn.workers.UvicornWorker \
    --max-requests 800 \
    --max-requests-jitter 200 \
    --timeout 30 \
    --graceful-timeout 20 \
    --keep-alive 5 \
    --access-logfile - \
    --log-level info"]
