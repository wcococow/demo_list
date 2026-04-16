import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from sqlalchemy import text
from sqlalchemy.orm import Session
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from database import SessionLocal, engine, Base
from schemas import ErrorResponse, TaskCreate, TaskResponse, TaskUpdate
import task_service

# ── logging ───────────────────────────────────────────────────────────────────

# Structured JSON logs are required so log aggregators (Datadog, CloudWatch,
# etc.) can index and search them. Plain print() output is invisible to most
# observability platforms.
logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}',
)
logger = logging.getLogger(__name__)

# ── rate limiting ─────────────────────────────────────────────────────────────

# Rate limiting prevents a single client from overwhelming the server.
# key_func=get_remote_address limits per IP. In production, swap this for
# a per-user-ID function once you have authentication in place.
limiter = Limiter(key_func=get_remote_address)

# ── auth ──────────────────────────────────────────────────────────────────────

# Simple API key guard. Set API_KEY in your environment to enable it.
# If API_KEY is not set, auth is skipped (dev mode only — always set it in prod).
_API_KEY = os.environ.get("API_KEY")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def require_api_key(key: str = Security(_api_key_header)):
    if _API_KEY and key != _API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

# ── CORS ──────────────────────────────────────────────────────────────────────

# Never use allow_origins=["*"] in production — it lets any website make
# authenticated requests on behalf of your users. Whitelist explicit origins.
_ALLOWED_ORIGINS = [o for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if o]

# ── lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # create_all is fine for dev. In production, use Alembic migrations instead
    # so schema changes are versioned and reversible.
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ready")
    yield
    logger.info("Shutting down")

# ── app ───────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Normalize all 4xx/5xx into the consistent ErrorResponse envelope so API
# consumers never have to handle two different error shapes.
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(error=str(exc.detail)).model_dump(),
    )


# ── DB dependency ─────────────────────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ── health ────────────────────────────────────────────────────────────────────

# Load balancers and Kubernetes readiness probes hit this endpoint.
# It checks real DB connectivity, not just that the process is alive.
@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        logger.exception("Health check: DB unavailable")
        raise HTTPException(status_code=503, detail="Database unavailable")

# ── routes ────────────────────────────────────────────────────────────────────

@app.post("/tasks", response_model=TaskResponse, status_code=201,
          dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute")  # prevent task-spam from a single IP
def create(request: Request, task: TaskCreate, db: Session = Depends(get_db)):
    logger.info("Creating task: %s", task.title)
    return task_service.create_task(db, task.title)


@app.get("/tasks", response_model=list[TaskResponse],
         dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute")
def get_all(
    request: Request,
    db: Session = Depends(get_db),
    skip: int = Query(default=0, ge=0),
    # le=100 caps the page size — without this a client can pass limit=1000000
    # and dump the entire table in one request
    limit: int = Query(default=20, ge=1, le=100),
):
    return task_service.get_all_tasks(db, skip=skip, limit=limit)


@app.get("/tasks/{task_id}", response_model=TaskResponse,
         dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute")
def get_one(request: Request, task_id: str, db: Session = Depends(get_db)):
    task = task_service.get_task_by_id(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.patch("/tasks/{task_id}", response_model=TaskResponse,
           dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute")
def update(request: Request, task_id: str, task: TaskUpdate,
           db: Session = Depends(get_db)):
    updated = task_service.update_task(db, task_id, title=task.title, is_done=task.is_done)
    if not updated:
        raise HTTPException(404, "Task not found")
    return updated


@app.delete("/tasks/{task_id}", status_code=204,
            dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute")
def delete(request: Request, task_id: str, db: Session = Depends(get_db)):
    success = task_service.delete_task(db, task_id)
    if not success:
        raise HTTPException(404, "Task not found")
