import asyncio
import logging
import time
import uuid as _uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import text
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
import os

REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT", "30"))

from alembic.config import Config as AlembicConfig
from alembic import command as alembic_command
from database import get_db, engine, Base
from schemas import ErrorResponse, JobResponse, JobStatus, TaskCreate, TaskResponse, TaskUpdate, Token, UserCreate, UserResponse
from models import User
import task_service
import user_service
from auth import create_access_token, get_current_user, get_session_id_from_token, oauth2_scheme
from session_manager import invalidate_session
from celery.result import AsyncResult
from task_jobs import create_task_job, update_task_job, delete_task_job
from opentelemetry import trace as otel_trace
from prometheus_fastapi_instrumentator import Instrumentator
from telemetry import setup_telemetry
from pythonjsonlogger import jsonlogger
from log_context import user_id_ctx

# ── logging ───────────────────────────────────────────────────────────────────
# Emits one JSON object per line — Promtail ships these to Loki,
# and Grafana can filter/aggregate by any field (user_id, trace_id, op, etc.)

class _StructuredFilter(logging.Filter):
    """Injects trace_id, span_id, and user_id into every log record."""
    def filter(self, record):
        span = otel_trace.get_current_span()
        ctx  = span.get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else ""
        record.span_id  = format(ctx.span_id,  "016x") if ctx.is_valid else ""
        record.user_id  = user_id_ctx.get("")
        return True

_handler = logging.StreamHandler()
_handler.setFormatter(jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s %(trace_id)s %(span_id)s %(user_id)s",
    rename_fields={"asctime": "time", "levelname": "level", "name": "logger"},
    datefmt="%Y-%m-%dT%H:%M:%S",
))
_handler.addFilter(_StructuredFilter())

logging.root.handlers = []
logging.root.addHandler(_handler)
logging.root.setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# ── rate limiting ─────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

# ── CORS ──────────────────────────────────────────────────────────────────────

_ALLOWED_ORIGINS = [o for o in os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000"
).split(",") if o]

# ── lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    from redis_client import redis_client as _redis
    # Distributed migration lock — only one pod runs alembic upgrade at a time
    acquired = _redis.set("db:migration_lock", "1", nx=True, ex=120)
    if acquired:
        try:
            _alembic_cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "alembic.ini"))
            _alembic_cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "alembic"))
            alembic_command.upgrade(_alembic_cfg, "head")
            logger.info("Database migrations applied")
        finally:
            _redis.delete("db:migration_lock")
    else:
        # Another pod is migrating — wait for it to finish before serving traffic
        for _ in range(30):
            if not _redis.exists("db:migration_lock"):
                break
            time.sleep(2)
        logger.info("Migrations complete (run by another pod)")
    setup_telemetry(app, engine)
    yield
    logger.info("Shutting down")

# ── app ───────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

# Expose /metrics for Prometheus — tracks KPIs: request rate, error rate, latency
Instrumentator(
    should_group_status_codes=False,   # keep 4xx/5xx separate for error rate KPI
    excluded_handlers=["/health", "/metrics"],
).instrument(app).expose(app)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

class TimeoutMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "Request timeout"}, status_code=504)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get("X-Request-ID", str(_uuid.uuid4()))
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response


class LoadTestTagMiddleware(BaseHTTPMiddleware):
    """
    Reads X-Load-Test-Run-Id from the request and stamps it as a span
    attribute so every span from a load test run is searchable in Jaeger
    by tag:  load_test.run_id = <value>
    No-ops on normal traffic that omits the header.
    """
    async def dispatch(self, request: Request, call_next):
        run_id = request.headers.get("X-Load-Test-Run-Id")
        if run_id:
            span = otel_trace.get_current_span()
            span.set_attribute("load_test.run_id", run_id)
            span.set_attribute("load_test.user", request.headers.get("X-Load-Test-User", ""))
            span.set_attribute("load_test.op", request.headers.get("X-Load-Test-Op", ""))
        return await call_next(request)


app.add_middleware(TimeoutMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(LoadTestTagMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(error=str(exc.detail)).model_dump(),
    )

# ── health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        logger.exception("Health check: DB unavailable")
        raise HTTPException(status_code=503, detail="Database unavailable")

# ── auth routes ───────────────────────────────────────────────────────────────

@app.post("/auth/register", response_model=UserResponse, status_code=201)
@limiter.limit("10/minute")
def register(request: Request, body: UserCreate, db: Session = Depends(get_db)):
    if user_service.get_user_by_username(db, body.username):
        raise HTTPException(409, "Username already taken")
    user = user_service.create_user(db, body.username, body.password)
    logger.info("user.registered", extra={"username": body.username, "user_id": user.id})
    return user


@app.post("/auth/login", response_model=Token)
@limiter.limit("20/minute")
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    try:
        token = user_service.authenticate_user(db, form.username, form.password)
    except ValueError as e:
        logger.warning("auth.locked", extra={"username": form.username})
        raise HTTPException(423, str(e))
    if not token:
        logger.warning("auth.failed", extra={"username": form.username})
        raise HTTPException(401, "Incorrect username or password")
    logger.info("auth.login", extra={"username": form.username})
    return Token(access_token=token)


@app.post("/auth/logout", status_code=204)
def logout(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    session_id = get_session_id_from_token(token)
    if session_id:
        invalidate_session(session_id)
    logger.info("auth.logout")


@app.post("/auth/refresh", response_model=Token)
def refresh(
    token: str = Depends(oauth2_scheme),
    current_user: User = Depends(get_current_user),
):
    from session_manager import refresh_session
    session_id = get_session_id_from_token(token)
    if session_id:
        refresh_session(session_id)
    logger.info("auth.refresh", extra={"user_id": current_user.id})
    return Token(access_token=create_access_token(current_user.id, session_id))

# ── task routes ───────────────────────────────────────────────────────────────

# ── job status ────────────────────────────────────────────────────────────────

_CELERY_TO_JOB_STATUS = {
    "PENDING": JobStatus.pending,
    "STARTED": JobStatus.started,
    "SUCCESS": JobStatus.success,
    "FAILURE": JobStatus.failed,
}

@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, _: User = Depends(get_current_user)):
    result = AsyncResult(job_id)
    status = _CELERY_TO_JOB_STATUS.get(result.status, JobStatus.pending)
    return JobResponse(
        job_id=job_id,
        status=status,
        result=result.result if result.status == "SUCCESS" else None,
    )

# ── task routes ───────────────────────────────────────────────────────────────

# Reads remain synchronous — users need data immediately
@app.get("/tasks", response_model=list[TaskResponse])
@limiter.limit("120/minute")
def get_all(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    return task_service.get_all_tasks(db, owner_id=current_user.id, skip=skip, limit=limit)


@app.get("/tasks/{task_id}", response_model=TaskResponse)
@limiter.limit("120/minute")
def get_one(request: Request, task_id: str, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    task = task_service.get_task_by_id(db, task_id)
    if not task or task.owner_id != current_user.id:
        raise HTTPException(404, "Task not found")
    return task


# Writes go through the queue — API returns 202 immediately, worker does the DB write
@app.post("/tasks", response_model=JobResponse, status_code=202)
@limiter.limit("60/minute")
def create(request: Request, task: TaskCreate,
           current_user: User = Depends(get_current_user)):
    job = create_task_job.delay(task.title, current_user.id)
    logger.info("task.create.enqueued", extra={"job_id": job.id, "title": task.title})
    return JobResponse(job_id=job.id, status=JobStatus.pending)


@app.patch("/tasks/{task_id}", response_model=JobResponse, status_code=202)
@limiter.limit("60/minute")
def update(request: Request, task_id: str, task: TaskUpdate,
           current_user: User = Depends(get_current_user)):
    job = update_task_job.delay(task_id, current_user.id,
                                title=task.title, is_done=task.is_done)
    logger.info("task.update.enqueued", extra={"job_id": job.id, "task_id": task_id,
                                               "title": task.title, "is_done": task.is_done})
    return JobResponse(job_id=job.id, status=JobStatus.pending)


@app.delete("/tasks/{task_id}", response_model=JobResponse, status_code=202)
@limiter.limit("60/minute")
def delete(request: Request, task_id: str,
           current_user: User = Depends(get_current_user)):
    job = delete_task_job.delay(task_id, current_user.id)
    logger.info("task.delete.enqueued", extra={"job_id": job.id, "task_id": task_id})
    return JobResponse(job_id=job.id, status=JobStatus.pending)
