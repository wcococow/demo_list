# Backend Architecture & Code Flow

## The Full Request Lifecycle

```
HTTP Request
     │
     ▼
┌─────────────────────────────────────────────┐
│  MIDDLEWARE LAYER  (main.py)                │
│                                             │
│  1. CORSMiddleware                          │
│     checks Origin header                   │
│     blocks if not in ALLOWED_ORIGINS        │
│                                             │
│  2. slowapi RateLimiter                     │
│     counts requests per IP                  │
│     returns 429 if over limit               │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  ROUTE HANDLER  (main.py)                  │
│                                             │
│  FastAPI matches URL + method               │
│  e.g. POST /tasks → def create(...)         │
└──────┬──────────┬───────────────────────────┘
       │          │
       │          ▼
       │  ┌───────────────────────────────┐
       │  │  DEPENDENCY INJECTION         │
       │  │                               │
       │  │  Depends(require_api_key)     │
       │  │    reads X-API-Key header     │
       │  │    raises 403 if wrong        │
       │  │                               │
       │  │  Depends(get_db)              │
       │  │    opens SessionLocal()       │
       │  │    yields db                  │
       │  │    closes db in finally       │
       │  └───────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────┐
│  SCHEMA VALIDATION  (schemas.py)            │
│                                             │
│  Pydantic deserialises request body         │
│  TaskCreate(title="Buy milk")               │
│    strip whitespace                         │
│    check min_length=1                       │
│    check max_length=255                     │
│  returns 422 automatically if invalid       │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  SERVICE LAYER  (task_service.py)           │
│                                             │
│  Pure Python — no HTTP, no FastAPI          │
│  receives (db, title)                       │
│  builds Task object                         │
│  calls db.add() / db.commit()               │
│  on error: db.rollback() then re-raises     │
│  returns Task ORM object                    │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  ORM / MODEL LAYER  (models.py)             │
│                                             │
│  Task maps to "tasks" table                 │
│  id       → STRING PRIMARY KEY (uuid)       │
│  title    → STRING NOT NULL                 │
│  is_done  → BOOLEAN (indexed)               │
│  created_at / updated_at → server clock     │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  DATABASE LAYER  (database.py)              │
│                                             │
│  DATABASE_URL from env                      │
│  SQLite (dev) or PostgreSQL (prod)          │
│  pool_pre_ping checks connection alive      │
│  pool_size controls max open connections    │
└─────────────────┬───────────────────────────┘
                  │
         SQL hits the DB
                  │
                  ▼
┌─────────────────────────────────────────────┐
│  RESPONSE SERIALISATION  (schemas.py)       │
│                                             │
│  FastAPI converts ORM object → dict         │
│  using TaskResponse (from_attributes=True)  │
│  only exposes: id, title, is_done,          │
│                created_at, updated_at       │
│  internal DB fields never leak out          │
└─────────────────┬───────────────────────────┘
                  │
                  ▼
            HTTP Response
```

---

## How The Files Own Each Layer

```
HTTP in/out         →  main.py          (routes, middleware, DI wiring)
Shape of data       →  schemas.py       (what comes in, what goes out)
Business logic      →  task_service.py  (all DB operations live here)
Table definition    →  models.py        (columns, indexes, timestamps)
Connection config   →  database.py      (URL, pool, session factory)
```

---

## The Rule For Where Code Goes

| Question | File |
|---|---|
| "What URL does this live at?" | `main.py` |
| "What fields does the API accept/return?" | `schemas.py` |
| "What happens to the data?" | `task_service.py` |
| "What does the DB table look like?" | `models.py` |
| "How do we connect to the DB?" | `database.py` |

---

## Design Patterns & Why They're Here

| Pattern | Where | Why |
|---|---|---|
| Repository | `task_service.py` | Isolates all DB calls — swap the ORM without touching routes |
| Dependency Injection | `main.py` via `Depends()` | Routes don't construct their own dependencies — makes testing trivial |
| DTO / Schema | `schemas.py` | Decouples API contract from DB model — internal columns never leak |
| Lifespan | `main.py` | Guarantees startup and shutdown always run as a pair |
| Middleware Chain | `main.py` | Cross-cutting concerns (CORS, rate limits) applied once, inherited by all routes |
| Guard / Policy | `require_api_key` | Auth declared at route level — visible without reading handler body |
| Unit of Work | `task_service.py` | Single `db.commit()` at the end of multi-step operations — all or nothing |
| Strategy | `key_func` in limiter | Swap rate-limit identity (IP → user ID) without changing enforcement logic |
| Config Object | `config.py` (to add) | One place for all env vars with type validation via Pydantic `BaseSettings` |

---

## Production Checklist

| Item | Status |
|---|---|
| `DATABASE_URL` set to PostgreSQL DSN | env var required |
| `API_KEY` set to a secret value | env var required |
| `ALLOWED_ORIGINS` set to real frontend domain | env var required |
| Alembic migrations replace `create_all()` | setup required |
| HTTPS terminated at reverse proxy (nginx/caddy) | infra required |
| Structured JSON logging to aggregator | configured in `main.py` |
| `/health` endpoint for load balancer probes | `main.py` |
| Rate limiting per IP (swap to per-user after auth) | `main.py` |
| Connection pool tuned for expected concurrency | `database.py` |
