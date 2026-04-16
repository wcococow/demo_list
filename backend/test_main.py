import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from main import app, get_db

# StaticPool forces all connections to share the same in-memory database.
# Without it, each new connection gets a blank :memory: database, so tables
# created by Base.metadata.create_all() vanish before the session sees them.
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture(autouse=True)
def reset_db():
    # Create all tables fresh before each test, drop them after — fully isolated
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
# Explicit dependency on reset_db guarantees tables exist before TestClient starts
def client(reset_db):
    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── helpers ──────────────────────────────────────────────────────────────────

def create_task(client, title="Buy milk"):
    return client.post("/tasks", json={"title": title})


# ── CREATE ────────────────────────────────────────────────────────────────────

def test_create_task_returns_201(client):
    res = create_task(client)
    assert res.status_code == 201
    body = res.json()
    assert body["title"] == "Buy milk"
    assert body["is_done"] is False
    assert "id" in body


def test_create_task_empty_title_rejected(client):
    # FIX #4 — empty title must be rejected at the schema level
    res = client.post("/tasks", json={"title": ""})
    assert res.status_code == 422


def test_create_task_whitespace_only_rejected(client):
    # FIX #4 — strip_whitespace means "   " collapses to "" and fails min_length
    res = client.post("/tasks", json={"title": "   "})
    assert res.status_code == 422


# ── READ ALL ──────────────────────────────────────────────────────────────────

def test_get_all_tasks_empty(client):
    res = client.get("/tasks")
    assert res.status_code == 200
    assert res.json() == []


def test_get_all_tasks_returns_created(client):
    create_task(client, "Task A")
    create_task(client, "Task B")
    res = client.get("/tasks")
    assert res.status_code == 200
    assert len(res.json()) == 2


def test_get_all_tasks_pagination(client):
    # FIX #5 — pagination keeps large tables from being returned whole
    for i in range(5):
        create_task(client, f"Task {i}")
    res = client.get("/tasks?skip=2&limit=2")
    assert res.status_code == 200
    assert len(res.json()) == 2


# ── READ ONE ──────────────────────────────────────────────────────────────────

def test_get_task_by_id(client):
    task_id = create_task(client).json()["id"]
    res = client.get(f"/tasks/{task_id}")
    assert res.status_code == 200
    assert res.json()["id"] == task_id


def test_get_task_not_found_returns_404(client):
    # FIX #6 — missing record must surface as 404, not 500
    res = client.get("/tasks/nonexistent-id")
    assert res.status_code == 404


# ── UPDATE ────────────────────────────────────────────────────────────────────

def test_update_task_title(client):
    task_id = create_task(client).json()["id"]
    res = client.patch(f"/tasks/{task_id}", json={"title": "Updated title"})
    assert res.status_code == 200
    assert res.json()["title"] == "Updated title"


def test_update_task_mark_done(client):
    task_id = create_task(client).json()["id"]
    res = client.patch(f"/tasks/{task_id}", json={"is_done": True})
    assert res.status_code == 200
    assert res.json()["is_done"] is True


def test_update_task_partial(client):
    # Patching only is_done should leave the title unchanged
    task_id = create_task(client, "Keep this title").json()["id"]
    res = client.patch(f"/tasks/{task_id}", json={"is_done": True})
    assert res.json()["title"] == "Keep this title"
    assert res.json()["is_done"] is True


def test_update_task_empty_title_rejected(client):
    # FIX #4 — blank title on update must also fail validation
    task_id = create_task(client).json()["id"]
    res = client.patch(f"/tasks/{task_id}", json={"title": ""})
    assert res.status_code == 422


def test_update_task_not_found_returns_404(client):
    # FIX #6
    res = client.patch("/tasks/nonexistent-id", json={"is_done": True})
    assert res.status_code == 404


# ── DELETE ────────────────────────────────────────────────────────────────────

def test_delete_task(client):
    task_id = create_task(client).json()["id"]
    res = client.delete(f"/tasks/{task_id}")
    assert res.status_code == 204


def test_delete_task_actually_removed(client):
    task_id = create_task(client).json()["id"]
    client.delete(f"/tasks/{task_id}")
    res = client.get(f"/tasks/{task_id}")
    assert res.status_code == 404


def test_delete_task_not_found_returns_404(client):
    # FIX #6
    res = client.delete("/tasks/nonexistent-id")
    assert res.status_code == 404
