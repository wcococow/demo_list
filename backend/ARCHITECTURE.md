# Backend Architecture & Code Flow

## Two Request Lifecycles

The API has two distinct paths depending on whether the operation reads or writes data.

### Read Path (synchronous — returns data immediately)

```
HTTP GET /tasks  or  GET /tasks/:id
          │
          ▼
┌─────────────────────────────────────────────┐
│  MIDDLEWARE LAYER  (main.py)                │
│                                             │
│  1. TimeoutMiddleware                       │
│     asyncio.wait_for(30s)                   │
│     returns 504 if exceeded                 │
│                                             │
│  2. CORSMiddleware                          │
│     checks Origin against ALLOWED_ORIGINS   │
│                                             │
│  3. slowapi RateLimiter (120 req/min)        │
│     per IP — returns 429 if over limit      │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  DEPENDENCY INJECTION  (main.py)            │
│                                             │
│  Depends(get_current_user)                  │
│    1. extracts Bearer token from header     │
│    2. decodes JWT → user_id + session_id    │
│    3. calls Redis: GET session:{session_id} │
│       → 401 if missing (logged out or TTL   │
│         expired)                            │
│    4. loads User row from PostgreSQL        │
│       → 401 if user deleted                 │
│                                             │
│  Depends(get_db)                            │
│    opens SessionLocal(), yields db,         │
│    closes in finally                        │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  SERVICE LAYER  (task_service.py)           │
│                                             │
│  Filters by owner_id = current_user.id      │
│  Users can only see their own tasks         │
│  Supports skip/limit pagination             │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  RESPONSE SERIALISATION  (schemas.py)       │
│                                             │
│  ORM object → TaskResponse                  │
│  Exposes: id, title, is_done, owner_id,     │
│           created_at, updated_at            │
│  Internal fields (hashed_password etc.)     │
│  never appear in any response               │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
             200 + JSON body
```

---

### Write Path (async — returns job handle immediately)

Writes use a task queue so the API never blocks on a slow DB write.
Under high load, requests are accepted and queued faster than the DB can process
them. Without this, a traffic spike would exhaust the DB connection pool and
cascade into 503s.

```
HTTP POST /tasks  (or PATCH, DELETE)
          │
          ▼
  [same middleware + auth as read path]
          │
          ▼
┌─────────────────────────────────────────────┐
│  ROUTE HANDLER  (main.py)                  │
│                                             │
│  Validates input (Pydantic 422 on failure)  │
│  Confirms user is authenticated             │
│  Calls: create_task_job.delay(title, uid)   │
│                                             │
│  .delay() pushes a message onto the         │
│  Redis "celery" list and returns instantly. │
│  No DB call happens here.                   │
│                                             │
│  Returns: 202 Accepted + { job_id, status } │
└─────────────────┬───────────────────────────┘
                  │ (async, in background)
                  ▼
┌─────────────────────────────────────────────┐
│  CELERY WORKER  (task_jobs.py)              │
│                                             │
│  Picks message off Redis broker (DB 1)      │
│  Opens its own DB session (SessionLocal)    │
│  Calls task_service.create_task(db, ...)    │
│  On success: stores result in Redis DB 1    │
│  On failure: retries up to 3× with          │
│    exponential backoff (2^n seconds)        │
│  After 3 failures: BaseTaskWithDLQ pushes   │
│    serialised error to Redis "dead_letter"  │
│    list (capped at 1000 entries)            │
└─────────────────────────────────────────────┘


Client polls until done:

GET /jobs/{job_id}
          │
          ▼
┌─────────────────────────────────────────────┐
│  AsyncResult(job_id)  (main.py line 153)    │
│                                             │
│  WHY AsyncResult?                           │
│  The job_id is the Celery task UUID.        │
│  AsyncResult wraps it and queries the       │
│  result backend (Redis DB 1) without        │
│  blocking a thread or opening a DB conn.    │
│                                             │
│  .status → one of:                          │
│    PENDING  — queued, not yet started       │
│    STARTED  — worker is running it          │
│    SUCCESS  — done; .result has the data    │
│    FAILURE  — failed after retries          │
│                                             │
│  The API maps these to JobStatus enum       │
│  and returns 200 + { job_id, status,        │
│  result } every time the client polls.      │
│  No long-polling or websockets needed.      │
└─────────────────────────────────────────────┘
```

---

## Auth Flow in Detail

```
POST /auth/register
  → hashes password with bcrypt (cost 12)
  → stores User row in PostgreSQL
  → returns UserResponse (no token yet)

POST /auth/login
  → checks is_account_locked(username) in Redis
     if true → 423 Locked (no password check)
  → loads User from DB
  → bcrypt.checkpw(plain, hashed)
     if wrong → record_failed_login(username)
                 increments Redis counter
                 5th failure → sets locked:{username}
                 key with 15min TTL
               → 401
  → clear_failed_logins(username)
  → create_session(user_id) in Redis
     key: session:{uuid}  value: user_id  TTL: 24h
  → create_access_token(user_id, session_id)
     JWT carries sub=user_id, sid=session_id
  → returns { access_token, token_type }

POST /auth/logout
  → decodes JWT to extract session_id
  → invalidate_session(session_id)
     deletes session:{uuid} from Redis immediately
  → token is now invalid even though JWT hasn't
    expired — this is why sessions live in Redis
    rather than relying on JWT expiry alone
```

---

## How The Files Own Each Layer

```
Request lifecycle    →  main.py           (routes, middleware, DI wiring)
Identity & tokens    →  auth.py           (JWT encode/decode, bcrypt)
Sessions & lockout   →  session_manager.py(Redis reads/writes for sessions)
Redis connection     →  redis_client.py   (single shared client instance)
Shape of data        →  schemas.py        (what the API accepts and returns)
User logic           →  user_service.py   (register, login, authenticate)
Task logic           →  task_service.py   (CRUD, ownership enforcement)
Async jobs           →  task_jobs.py      (Celery tasks wrapping service calls)
Celery config        →  celery_app.py     (broker URL, DLQ base class)
Tracing              →  telemetry.py      (OTel setup, W3C traceparent)
Table definitions    →  models.py         (columns, indexes, relationships)
Connection config    →  database.py       (URL, pool, session factory)
```

---

## The Rule For Where Code Goes

| Question | File |
|---|---|
| "What URL does this live at?" | `main.py` |
| "What fields does the API accept/return?" | `schemas.py` |
| "What happens to the data?" | `task_service.py` / `user_service.py` |
| "Where is the job actually executed?" | `task_jobs.py` |
| "What does the DB table look like?" | `models.py` |
| "How do we connect to the DB?" | `database.py` |
| "How do we validate a token?" | `auth.py` |
| "Where is the session stored?" | `session_manager.py` (Redis) |

---

## Design Patterns & Why They're Here

| Pattern | Where | Why |
|---|---|---|
| Repository | `task_service.py` | Isolates all DB calls — swap the ORM without touching routes |
| Dependency Injection | `main.py` via `Depends()` | Routes don't construct their own dependencies — trivial to override in tests |
| DTO / Schema | `schemas.py` | Decouples API contract from DB model — internal columns never leak |
| Command Queue | `task_jobs.py` + Celery | Writes are enqueued and processed at DB's pace — API never blocks on slow writes |
| Polling / AsyncResult | `GET /jobs/{id}` | Client polls lightweight Redis reads instead of holding an open HTTP connection |
| Token + Session | `auth.py` + `session_manager.py` | JWT carries identity; Redis session enables instant revocation on logout |
| Fail-Fast Config | `auth.py` | Raises `RuntimeError` at startup if `SECRET_KEY` is missing — never runs insecure |
| Dead Letter Queue | `celery_app.py` | Failed jobs after retries are preserved in Redis for inspection, not silently dropped |
| Distributed Tracing | `telemetry.py` | W3C `traceparent` header from frontend stitches browser → API → worker spans in Jaeger |
| Lifespan | `main.py` | Migrations + telemetry setup run once at startup, never inside a request |
| Middleware Chain | `main.py` | Cross-cutting concerns (timeout, CORS, rate limits) applied once, inherited by all routes |

---

## Production Checklist

| Item | Status |
|---|---|
| `DATABASE_URL` set to PostgreSQL DSN | env var required |
| `SECRET_KEY` set to random 256-bit value | env var required — app refuses to start without it |
| `REDIS_URL` set to Redis instance | env var required |
| `ALLOWED_ORIGINS` set to real frontend domain | env var required |
| Alembic migrations run on startup | automatic via lifespan |
| HTTPS via cert-manager + Let's Encrypt | `k8s/12-cert-manager.yml` |
| Structured JSON logging → Loki | configured in `main.py` + `loki/` |
| Prometheus metrics at `/metrics` | `prometheus-fastapi-instrumentator` |
| Grafana dashboards + alerting rules | `grafana/provisioning/` |
| Distributed tracing → Jaeger | `telemetry.py` (OTel + OTLP/gRPC) |
| Account lockout after 5 bad passwords | `session_manager.py` |
| Request timeout (default 30s) | `TimeoutMiddleware` in `main.py` |
| Write operations queued (non-blocking) | Celery + Redis broker |
| Dead letter queue for failed jobs | `celery_app.py` `BaseTaskWithDLQ` |
| Worker auto-scaling on queue depth | `k8s/06-keda-worker-scaler.yml` |
| API horizontal scaling (CPU-based) | `k8s/04-api.yml` HPA |
| Zero-downtime rolling deploys | `RollingUpdate` + `maxUnavailable: 0` |
| Daily DB backups | `k8s/13-db-backup.yml` CronJob |
| CI: test → build → push → deploy | `.github/workflows/ci.yml` |
