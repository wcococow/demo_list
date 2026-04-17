# Changelog

---

## v1.4.0 — Structured JSON Logging with User ID (2026-04-17)

### Changes

| # | File | What changed | Why |
|---|---|---|---|
| 1 | `backend/log_context.py` | New file — `ContextVar[str]` for `user_id` | A `ContextVar` is request-scoped in async Python; setting it once in `get_current_user` makes `user_id` available to every log call in that request without passing it explicitly |
| 2 | `backend/requirements.txt` | Added `python-json-logger==2.0.7` | Emits one JSON object per log line; Promtail ships these to Loki where every field (`user_id`, `trace_id`, `op`) is queryable |
| 3 | `backend/main.py` | Replaced `logging.basicConfig` with a JSON handler + `_StructuredFilter` | The filter injects `trace_id` and `span_id` from the active OTel span, and `user_id` from the ContextVar, into every log record automatically |
| 4 | `backend/auth.py` | `get_current_user` calls `user_id_ctx.set(user.id)` | Sets the ContextVar once after auth so all subsequent log calls in the same request carry the correct `user_id` without any extra work |
| 5 | `backend/main.py` | Added `logger.info/warning` at every auth and task route | Records `user.registered`, `auth.login`, `auth.failed`, `auth.logout`, `auth.refresh`, `task.create.enqueued`, `task.update.enqueued`, `task.delete.enqueued` with relevant fields |
| 6 | `backend/task_jobs.py` | Added `logger.info/error` in every Celery task | Worker logs `task.created`, `task.updated`, `task.deleted` (success) and `task.*.failed` (error) with `user_id`, `task_id`, and error message |

### Grafana / Loki queries

```logql
# all events for one user
{container=~".*api.*"} | json | user_id="abc-123"

# all failed logins in the last hour
{container=~".*api.*"} | json | message="auth.failed"

# full journey for one trace (correlate logs ↔ Jaeger)
{container=~".*api.*"} | json | trace_id="a3f2b1c9..."

# task errors across api + worker
{container=~".*api.*|.*worker.*"} | json | message=~".*failed"
```

---

## v1.3.0 — Load Test Run-Level Trace Search (2026-04-17)

### Changes

| # | File | What changed | Why |
|---|---|---|---|
| 1 | `backend/main.py` | Added `LoadTestTagMiddleware` — reads `X-Load-Test-Run-Id`, `X-Load-Test-User`, `X-Load-Test-Op` headers and calls `span.set_attribute()` | Span attributes are indexed by Jaeger; without them you can only look up one trace at a time by its ID — with them you search `load_test.run_id=<RUN_ID>` and see every span from all 10 000 users in one query |
| 2 | `backend/load_test.sh` | Every curl call now sends `X-Load-Test-Run-Id`, `X-Load-Test-User`, `X-Load-Test-Op` headers | Passes the run/user/operation context to the middleware above |
| 3 | `backend/load_test.sh` | Summary block prints the exact Jaeger tag search instructions | Removes guesswork — user copies the printed `load_test.run_id=<value>` tag directly into Jaeger |

---

## v1.2.0 — Load Test with Jaeger Trace Propagation (2026-04-17)

### Changes

| # | File | What changed | Why |
|---|---|---|---|
| 1 | `backend/load_test.sh` | New script — 10 000 concurrent users, each: register → login → create task (async, polls `/jobs/:id`) → random op (update / complete / delete) | Generates real production-shape traffic to stress test the full async stack |
| 2 | `backend/load_test.sh` | Every request carries a `traceparent: 00-<trace_id>-<span_id>-01` W3C header | FastAPI OTel middleware reads this header and links all spans (API → DB → Redis → Celery worker) under the same trace in Jaeger — without it each request appears as an isolated root trace |
| 3 | `backend/load_test.sh` | Each user gets one `trace_id`, each HTTP call gets a fresh `span_id` (child of same trace) | Matches real browser behaviour: one page load = one trace, each fetch = one span |
| 4 | `backend/load_test.sh` | Concurrency controlled via `jobs -r` — throttles when running jobs ≥ `CONCURRENCY` | Prevents fork-bombing the machine; default 100 concurrent workers, overridable with `CONCURRENCY=N ./load_test.sh` |
| 5 | `backend/load_test.sh` | Atomic per-user result files (`$OK_DIR/$n`, `$FAIL_DIR/$n`) for counting | Multiple bash subprocesses can't safely increment a shared variable; touching unique files is atomic and race-free |
| 6 | `backend/load_test.sh` | Background progress reporter prints live `done / total` + pass/fail counts every second | Gives visibility into test progress without blocking the main loop |
| 7 | `backend/load_test.sh` | Prints Jaeger deep-link per trace at end: `localhost:16686/trace/<trace_id>` | One-click to open the exact trace in Jaeger without having to search manually |

---

---

## v1.1.0 — Production Hardening (2026-04-17)

### Changes

| # | File | What changed | Why |
|---|---|---|---|
| 1 | `session_manager.py` | `redis_client.keys()` → `scan_iter(..., count=100)` | `KEYS` blocks the entire Redis server while it scans every key in the keyspace — O(N) on total keys, not just session keys. `SCAN` iterates in small batches so Redis keeps serving other clients between chunks. |
| 2 | `requirements.txt` | Replaced `python-jose[cryptography]` with `PyJWT==2.9.0` | `python-jose` has had unpatched CVEs since 2023 and is effectively unmaintained. PyJWT is the actively maintained standard. |
| 3 | `requirements.txt` | Replaced `psycopg2-binary` with `psycopg2` | The `binary` variant bundles its own libpq so it can't use system security patches. The compiled package links against the OS-provided `libpq5`, which gets patched automatically. |
| 4 | `requirements.txt` | Added `gunicorn==22.0.0` | uvicorn alone is a single-process server. Gunicorn is a production process manager that spawns multiple uvicorn worker processes, handles restarts on crash, and manages graceful shutdown. |
| 5 | `requirements.txt` | Removed `httpx`, `pytest`, `pytest-mock` | Test tools were being installed into the production Docker image, adding ~30 MB of attack surface with no benefit. |
| 6 | `requirements-dev.txt` | New file — test dependencies | Separates runtime from test deps. CI/dev installs `requirements-dev.txt`; the production Docker image installs `requirements.txt` only. |
| 7 | `Dockerfile` | Multi-stage build (builder + runtime stages) | The builder stage installs `gcc` + `libpq-dev` to compile psycopg2. The runtime stage copies only the installed packages — no compiler in the final image, reducing size and attack surface. |
| 8 | `Dockerfile` | Added non-root `appuser` | Running as root inside a container means a container escape gives full host access. A dedicated non-root user limits the blast radius. |
| 9 | `Dockerfile` | Added `HEALTHCHECK` instruction | Without `HEALTHCHECK`, Docker (and Kubernetes with `livenessProbe`) can't tell if the app has crashed but the process is still running. The check hits `/health` every 10 s. |
| 10 | `Dockerfile` | `CMD` changed to gunicorn with 4 uvicorn workers + `--access-logfile -` + graceful-timeout | 4 workers handle concurrent requests. Access logs go to stdout so Promtail/Loki picks them up. `--graceful-timeout 30` lets in-flight requests finish before shutdown. |
| 11 | `.dockerignore` | New file | Without it, `docker build` copies `.env`, `test_*.py`, `conftest.py`, and `__pycache__` into the image layer. `.env` in a Docker layer is a credential leak even if deleted in a later layer. |
| 12 | `auth.py` | Replaced `from jose import JWTError, jwt` with `import jwt` (PyJWT) | Follows the `requirements.txt` library swap. API is identical for HS256; exception class changes from `JWTError` to `jwt.PyJWTError`. |
| 13 | `database.py` | `DATABASE_URL` now raises `RuntimeError` if unset | Previously fell back to `sqlite:///./tasks.db`. In a pod with no persistent volume, a missing env var would silently create a local SQLite file that vanishes on restart — undetectable data loss. Fail-fast is safer. |
| 14 | `main.py` | Redis distributed lock around Alembic migrations in `lifespan` | With 2+ API replicas all starting at the same time, each runs `alembic upgrade head` simultaneously — this causes constraint errors and race conditions on cold deploys. One pod acquires a `SET NX` Redis lock; others wait up to 60 s for it to finish. |
| 15 | `main.py` | Added `RequestIDMiddleware` (X-Request-ID) | Echoes or generates a `X-Request-ID` header on every response. Clients can pass their own ID to correlate a request across logs, OTel traces, and Celery jobs without opening the Jaeger UI. |
| 16 | `main.py` | Added `POST /auth/refresh` endpoint | A 24-hour token forces users to re-login daily. `/auth/refresh` issues a new token + extends the session TTL while the current session is still valid — no password re-entry needed. |
