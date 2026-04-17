# ── stage 1: build dependencies ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

# gcc + libpq-dev needed to compile psycopg2 from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── stage 2: lean runtime image ───────────────────────────────────────────────
FROM python:3.12-slim

# libpq5 = runtime PostgreSQL client (psycopg2 links against it)
# curl   = used by HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

RUN adduser --disabled-password --gecos "" appuser

WORKDIR /app
COPY . .
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["gunicorn", "main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "60", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-"]
