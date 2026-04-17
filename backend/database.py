
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

_is_sqlite = DATABASE_URL.startswith("sqlite")

# SQLite needs check_same_thread=False because FastAPI runs handlers in
# a thread pool. PostgreSQL doesn't need this arg.
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

# pool_pre_ping=True checks connection health before handing it to a request.
# This prevents "server closed the connection" errors after idle periods.

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    **({} if _is_sqlite else {
        "pool_size": 10,       # persistent connections kept open
        "max_overflow": 20,    # extra connections allowed under burst load
        "pool_timeout": 30,    # seconds to wait before raising on no conn available
    })
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

Base = declarative_base()
# pool_size / max_overflow only apply to PostgreSQL — SQLite ignores them.


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()