import uuid

from prometheus_client import Gauge
from redis_client import redis_client

SESSION_TTL_SECONDS = 60 * 60 * 24
_PREFIX = "session:"

_LOCKOUT_THRESHOLD = 5
_LOCKOUT_TTL = 15 * 60   # 15 minutes
_ATTEMPT_TTL  = 15 * 60


def _failed_key(username: str) -> str: return f"failed_logins:{username}"
def _lock_key(username: str)   -> str: return f"locked:{username}"


# ── account lockout ───────────────────────────────────────────────────────────

def is_account_locked(username: str) -> bool:
    return bool(redis_client.exists(_lock_key(username)))


def record_failed_login(username: str) -> None:
    key = _failed_key(username)
    count = redis_client.incr(key)
    redis_client.expire(key, _ATTEMPT_TTL)
    if count >= _LOCKOUT_THRESHOLD:
        redis_client.setex(_lock_key(username), _LOCKOUT_TTL, "1")


def clear_failed_logins(username: str) -> None:
    redis_client.delete(_failed_key(username), _lock_key(username))


# ── sessions ──────────────────────────────────────────────────────────────────

def _count_active_sessions() -> int:
    # SCAN iterates in small chunks — never blocks Redis, unlike KEYS
    return sum(1 for _ in redis_client.scan_iter(f"{_PREFIX}*", count=100))


# Initialise from Redis so the gauge survives API restarts
active_sessions = Gauge(
    "active_sessions_total",
    "Number of currently active user sessions",
)
active_sessions.set_function(_count_active_sessions)


def create_session(user_id: str) -> str:
    session_id = str(uuid.uuid4())
    redis_client.setex(f"{_PREFIX}{session_id}", SESSION_TTL_SECONDS, user_id)
    return session_id


def get_session_user_id(session_id: str) -> str | None:
    return redis_client.get(f"{_PREFIX}{session_id}")


def invalidate_session(session_id: str) -> None:
    redis_client.delete(f"{_PREFIX}{session_id}")


def refresh_session(session_id: str) -> bool:
    return bool(redis_client.expire(f"{_PREFIX}{session_id}", SESSION_TTL_SECONDS))
