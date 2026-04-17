"""
API-layer tests for main.py.
Uses FastAPI TestClient with SQLite (StaticPool) and mocked Redis/Celery.
"""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from models import User
from auth import create_access_token, hash_password
import uuid


# ── app & DB setup ────────────────────────────────────────────────────────────

_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


def _override_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="session")
def app_client():
    """One TestClient for the whole session; tables are managed per-test."""
    Base.metadata.create_all(bind=_engine)

    from main import app
    app.dependency_overrides[get_db] = _override_db

    with patch("alembic.command.upgrade"):       # skip migrations in lifespan
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture(autouse=True)
def clean_tables():
    """Wipe all rows before each test for isolation."""
    yield
    db = _Session()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()
    finally:
        db.close()


# ── auth helpers ──────────────────────────────────────────────────────────────

def _make_user(username="tester") -> tuple[User, str]:
    """Insert a user into the in-memory DB and return (user, jwt_token)."""
    db = _Session()
    user = User(id=str(uuid.uuid4()), username=username, hashed_password=hash_password("secret123"))
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    session_id = str(uuid.uuid4())
    token = create_access_token(user.id, session_id)
    return user, token, session_id


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── health ────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_ok(self, app_client):
        r = app_client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ── register ──────────────────────────────────────────────────────────────────

class TestRegister:
    def test_success(self, app_client):
        with patch("user_service.create_session", return_value="s1"), \
             patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.clear_failed_logins"):
            r = app_client.post("/auth/register", json={"username": "alice", "password": "pass123"})
        assert r.status_code == 201
        assert r.json()["username"] == "alice"
        assert "id" in r.json()

    def test_duplicate_returns_409(self, app_client):
        with patch("user_service.create_session", return_value="s"), \
             patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.clear_failed_logins"):
            app_client.post("/auth/register", json={"username": "dup", "password": "pass123"})
            r = app_client.post("/auth/register", json={"username": "dup", "password": "pass123"})
        assert r.status_code == 409

    def test_short_password_rejected(self, app_client):
        r = app_client.post("/auth/register", json={"username": "bob", "password": "x"})
        assert r.status_code == 422

    def test_empty_username_rejected(self, app_client):
        r = app_client.post("/auth/register", json={"username": "", "password": "pass123"})
        assert r.status_code == 422


# ── login ─────────────────────────────────────────────────────────────────────

class TestLogin:
    def _register(self, client, username="loginuser"):
        with patch("user_service.create_session", return_value="s"), \
             patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.clear_failed_logins"):
            client.post("/auth/register", json={"username": username, "password": "secret123"})

    def test_success_returns_token(self, app_client):
        self._register(app_client)
        with patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.clear_failed_logins"), \
             patch("user_service.create_session", return_value="sess-x"):
            r = app_client.post("/auth/login", data={"username": "loginuser", "password": "secret123"})
        assert r.status_code == 200
        assert "access_token" in r.json()
        assert r.json()["token_type"] == "bearer"

    def test_wrong_password_returns_401(self, app_client):
        self._register(app_client)
        with patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.record_failed_login"):
            r = app_client.post("/auth/login", data={"username": "loginuser", "password": "wrong"})
        assert r.status_code == 401

    def test_unknown_user_returns_401(self, app_client):
        with patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.record_failed_login"):
            r = app_client.post("/auth/login", data={"username": "nobody", "password": "pass"})
        assert r.status_code == 401

    def test_locked_account_returns_423(self, app_client):
        self._register(app_client)
        with patch("user_service.is_account_locked", return_value=True):
            r = app_client.post("/auth/login", data={"username": "loginuser", "password": "secret123"})
        assert r.status_code == 423


# ── logout ────────────────────────────────────────────────────────────────────

class TestLogout:
    def test_success(self, app_client):
        user, token, session_id = _make_user("logout_user")
        with patch("session_manager.get_session_user_id", return_value=user.id), \
             patch("session_manager.invalidate_session") as mock_inv:
            r = app_client.post("/auth/logout", headers=_auth(token))
        assert r.status_code == 204

    def test_no_token_returns_401(self, app_client):
        r = app_client.post("/auth/logout")
        assert r.status_code == 401


# ── GET /tasks ────────────────────────────────────────────────────────────────

class TestGetTasks:
    def test_unauthenticated_returns_401(self, app_client):
        r = app_client.get("/tasks")
        assert r.status_code == 401

    def test_expired_session_returns_401(self, app_client):
        user, token, session_id = _make_user("tasks_user_exp")
        with patch("session_manager.get_session_user_id", return_value=None):
            r = app_client.get("/tasks", headers=_auth(token))
        assert r.status_code == 401

    def test_returns_empty_list(self, app_client):
        user, token, session_id = _make_user("tasks_empty")
        with patch("session_manager.get_session_user_id", return_value=user.id):
            r = app_client.get("/tasks", headers=_auth(token))
        assert r.status_code == 200
        assert r.json() == []

    def test_pagination_limit_enforced(self, app_client):
        user, token, session_id = _make_user("tasks_page")
        with patch("session_manager.get_session_user_id", return_value=user.id):
            r = app_client.get("/tasks?limit=999", headers=_auth(token))
        assert r.status_code == 422


# ── GET /tasks/:id ────────────────────────────────────────────────────────────

class TestGetOneTask:
    def test_unauthenticated_returns_401(self, app_client):
        assert app_client.get("/tasks/some-id").status_code == 401

    def test_nonexistent_returns_404(self, app_client):
        user, token, _ = _make_user("getone_user")
        with patch("session_manager.get_session_user_id", return_value=user.id):
            r = app_client.get("/tasks/nonexistent", headers=_auth(token))
        assert r.status_code == 404


# ── POST /tasks ───────────────────────────────────────────────────────────────

class TestCreateTask:
    def test_returns_202_with_job_id(self, app_client):
        user, token, _ = _make_user("create_user")
        mock_job = MagicMock()
        mock_job.id = "job-abc"
        with patch("session_manager.get_session_user_id", return_value=user.id), \
             patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = mock_job
            r = app_client.post("/tasks", json={"title": "Buy milk"}, headers=_auth(token))
        assert r.status_code == 202
        assert r.json()["job_id"] == "job-abc"
        assert r.json()["status"] == "pending"

    def test_unauthenticated_returns_401(self, app_client):
        assert app_client.post("/tasks", json={"title": "Buy milk"}).status_code == 401

    def test_empty_title_returns_422(self, app_client):
        user, token, _ = _make_user("create_val")
        with patch("session_manager.get_session_user_id", return_value=user.id):
            r = app_client.post("/tasks", json={"title": ""}, headers=_auth(token))
        assert r.status_code == 422

    def test_whitespace_title_returns_422(self, app_client):
        user, token, _ = _make_user("create_ws")
        with patch("session_manager.get_session_user_id", return_value=user.id):
            r = app_client.post("/tasks", json={"title": "   "}, headers=_auth(token))
        assert r.status_code == 422


# ── PATCH /tasks/:id ──────────────────────────────────────────────────────────

class TestUpdateTask:
    def test_returns_202_with_job_id(self, app_client):
        user, token, _ = _make_user("upd_user")
        mock_job = MagicMock()
        mock_job.id = "job-upd"
        with patch("session_manager.get_session_user_id", return_value=user.id), \
             patch("main.update_task_job") as mock_celery:
            mock_celery.delay.return_value = mock_job
            r = app_client.patch("/tasks/some-id", json={"is_done": True}, headers=_auth(token))
        assert r.status_code == 202
        assert r.json()["job_id"] == "job-upd"

    def test_unauthenticated_returns_401(self, app_client):
        assert app_client.patch("/tasks/some-id", json={"is_done": True}).status_code == 401

    def test_empty_title_returns_422(self, app_client):
        user, token, _ = _make_user("upd_val")
        with patch("session_manager.get_session_user_id", return_value=user.id):
            r = app_client.patch("/tasks/some-id", json={"title": ""}, headers=_auth(token))
        assert r.status_code == 422


# ── DELETE /tasks/:id ─────────────────────────────────────────────────────────

class TestDeleteTask:
    def test_returns_202_with_job_id(self, app_client):
        user, token, _ = _make_user("del_user")
        mock_job = MagicMock()
        mock_job.id = "job-del"
        with patch("session_manager.get_session_user_id", return_value=user.id), \
             patch("main.delete_task_job") as mock_celery:
            mock_celery.delay.return_value = mock_job
            r = app_client.delete("/tasks/some-id", headers=_auth(token))
        assert r.status_code == 202
        assert r.json()["job_id"] == "job-del"

    def test_unauthenticated_returns_401(self, app_client):
        assert app_client.delete("/tasks/some-id").status_code == 401


# ── GET /jobs/:id ─────────────────────────────────────────────────────────────

class TestJobStatus:
    def test_pending_job(self, app_client):
        user, token, _ = _make_user("jobs_pend")
        mock_result = MagicMock()
        mock_result.status = "PENDING"
        mock_result.result = None
        with patch("session_manager.get_session_user_id", return_value=user.id), \
             patch("main.AsyncResult", return_value=mock_result):
            r = app_client.get("/jobs/job-123", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["status"] == "pending"

    def test_success_job_includes_result(self, app_client):
        user, token, _ = _make_user("jobs_ok")
        mock_result = MagicMock()
        mock_result.status = "SUCCESS"
        mock_result.result = {"id": "t1", "title": "Done"}
        with patch("session_manager.get_session_user_id", return_value=user.id), \
             patch("main.AsyncResult", return_value=mock_result):
            r = app_client.get("/jobs/job-456", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["status"] == "success"
        assert r.json()["result"] is not None

    def test_failed_job(self, app_client):
        user, token, _ = _make_user("jobs_fail")
        mock_result = MagicMock()
        mock_result.status = "FAILURE"
        mock_result.result = None
        with patch("session_manager.get_session_user_id", return_value=user.id), \
             patch("main.AsyncResult", return_value=mock_result):
            r = app_client.get("/jobs/job-789", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["status"] == "failed"

    def test_unauthenticated_returns_401(self, app_client):
        assert app_client.get("/jobs/job-123").status_code == 401
