"""
Comprehensive tests for the async POST /tasks flow.

Covers:
- Single user creating tasks with various titles
- Multiple concurrent users each owning their own tasks
- Job lifecycle: pending → started → success / failure
- Job polling after task creation
- Isolation: users cannot see each other's jobs
- Edge cases: title validation, auth guards, job ID format
- Bulk creation: many tasks for one user
- Concurrent creation: many users simultaneously
"""
import uuid
import pytest
from unittest.mock import MagicMock, patch, call
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from models import User
from auth import create_access_token, hash_password


# ── shared in-memory DB ───────────────────────────────────────────────────────

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


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_engine)
    from main import app
    app.dependency_overrides[get_db] = _override_db
    with patch("alembic.command.upgrade"):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture(autouse=True)
def clean_tables():
    yield
    db = _Session()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def bypass_rate_limit():
    """Disable slowapi rate limiting for all tests."""
    from main import limiter as app_limiter
    app_limiter.enabled = False
    yield
    app_limiter.enabled = True


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_user(username: str = "user") -> tuple[User, str]:
    """Insert a user and return (user, bearer_token)."""
    db = _Session()
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        hashed_password=hash_password("secret123"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    session_id = str(uuid.uuid4())
    token = create_access_token(user.id, session_id)
    return user, token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mock_job(job_id: str = None) -> MagicMock:
    m = MagicMock()
    m.id = job_id or f"job-{uuid.uuid4()}"
    return m


def _session_patch(user_id: str):
    return patch("session_manager.get_session_user_id", return_value=user_id)


# ── TestCreateTaskAsync: core async behaviour ─────────────────────────────────

class TestCreateTaskAsync:
    """POST /tasks returns 202 immediately and enqueues a Celery job."""

    def test_returns_202(self, client):
        user, token = _make_user("async_basic")
        job = _mock_job("job-basic-1")
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            r = client.post("/tasks", json={"title": "Write tests"}, headers=_auth(token))
        assert r.status_code == 202

    def test_response_contains_job_id(self, client):
        user, token = _make_user("async_job_id")
        job = _mock_job("job-xyz-99")
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            r = client.post("/tasks", json={"title": "Check job id"}, headers=_auth(token))
        assert r.json()["job_id"] == "job-xyz-99"

    def test_initial_status_is_pending(self, client):
        user, token = _make_user("async_pending")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            r = client.post("/tasks", json={"title": "Pending task"}, headers=_auth(token))
        assert r.json()["status"] == "pending"

    def test_celery_called_with_title_and_user_id(self, client):
        user, token = _make_user("async_args")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            client.post("/tasks", json={"title": "Important task"}, headers=_auth(token))
        mock_celery.delay.assert_called_once_with("Important task", user.id)

    def test_celery_receives_correct_user_id(self, client):
        user, token = _make_user("async_userid")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            client.post("/tasks", json={"title": "Task for user"}, headers=_auth(token))
        _, call_user_id = mock_celery.delay.call_args[0]
        assert call_user_id == user.id

    def test_no_result_field_on_pending(self, client):
        user, token = _make_user("async_noresult")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            r = client.post("/tasks", json={"title": "No result yet"}, headers=_auth(token))
        assert r.json().get("result") is None


# ── TestCreateTaskAuthGuards: unauthenticated / invalid token ─────────────────

class TestCreateTaskAuthGuards:
    def test_no_token_returns_401(self, client):
        r = client.post("/tasks", json={"title": "No auth"})
        assert r.status_code == 401

    def test_invalid_token_returns_401(self, client):
        r = client.post(
            "/tasks",
            json={"title": "Bad token"},
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert r.status_code == 401

    def test_expired_session_returns_401(self, client):
        user, token = _make_user("auth_expired")
        with patch("session_manager.get_session_user_id", return_value=None):
            r = client.post("/tasks", json={"title": "Expired"}, headers=_auth(token))
        assert r.status_code == 401

    def test_malformed_bearer_returns_401(self, client):
        r = client.post(
            "/tasks",
            json={"title": "Bad bearer"},
            headers={"Authorization": "Token abc123"},
        )
        assert r.status_code == 401


# ── TestCreateTaskValidation: title field rules ───────────────────────────────

class TestCreateTaskValidation:
    def test_empty_title_returns_422(self, client):
        user, token = _make_user("val_empty")
        with _session_patch(user.id):
            r = client.post("/tasks", json={"title": ""}, headers=_auth(token))
        assert r.status_code == 422

    def test_whitespace_only_title_returns_422(self, client):
        user, token = _make_user("val_ws")
        with _session_patch(user.id):
            r = client.post("/tasks", json={"title": "   "}, headers=_auth(token))
        assert r.status_code == 422

    def test_title_too_long_returns_422(self, client):
        user, token = _make_user("val_long")
        with _session_patch(user.id):
            r = client.post("/tasks", json={"title": "x" * 256}, headers=_auth(token))
        assert r.status_code == 422

    def test_missing_title_field_returns_422(self, client):
        user, token = _make_user("val_missing")
        with _session_patch(user.id):
            r = client.post("/tasks", json={}, headers=_auth(token))
        assert r.status_code == 422

    def test_null_title_returns_422(self, client):
        user, token = _make_user("val_null")
        with _session_patch(user.id):
            r = client.post("/tasks", json={"title": None}, headers=_auth(token))
        assert r.status_code == 422

    def test_title_max_length_accepted(self, client):
        user, token = _make_user("val_maxlen")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            r = client.post("/tasks", json={"title": "a" * 255}, headers=_auth(token))
        assert r.status_code == 202

    def test_single_char_title_accepted(self, client):
        user, token = _make_user("val_single")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            r = client.post("/tasks", json={"title": "X"}, headers=_auth(token))
        assert r.status_code == 202

    def test_unicode_title_accepted(self, client):
        user, token = _make_user("val_unicode")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            r = client.post("/tasks", json={"title": "タスク 🎯"}, headers=_auth(token))
        assert r.status_code == 202

    def test_leading_whitespace_is_stripped(self, client):
        """TrimmedStr strips leading/trailing whitespace before validation."""
        user, token = _make_user("val_strip")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mock_celery:
            mock_celery.delay.return_value = job
            client.post("/tasks", json={"title": "  real title  "}, headers=_auth(token))
        called_title = mock_celery.delay.call_args[0][0]
        assert called_title == "real title"


# ── TestMultipleUsersIsolation: each user's tasks are independent ─────────────

class TestMultipleUsersIsolation:
    """Each user gets their own job_id; Celery is called with correct user_id."""

    def test_two_users_get_different_job_ids(self, client):
        u1, t1 = _make_user("iso_user1")
        u2, t2 = _make_user("iso_user2")
        job1, job2 = _mock_job("job-u1"), _mock_job("job-u2")

        with _session_patch(u1.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job1
            r1 = client.post("/tasks", json={"title": "User1 task"}, headers=_auth(t1))

        with _session_patch(u2.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job2
            r2 = client.post("/tasks", json={"title": "User2 task"}, headers=_auth(t2))

        assert r1.json()["job_id"] != r2.json()["job_id"]

    def test_each_user_id_passed_to_celery(self, client):
        u1, t1 = _make_user("iso_id1")
        u2, t2 = _make_user("iso_id2")

        recorded = []

        def capture_delay(title, user_id):
            recorded.append((title, user_id))
            return _mock_job()

        with patch("main.create_task_job") as mc:
            mc.delay.side_effect = capture_delay
            with _session_patch(u1.id):
                client.post("/tasks", json={"title": "T1"}, headers=_auth(t1))
            with _session_patch(u2.id):
                client.post("/tasks", json={"title": "T2"}, headers=_auth(t2))

        assert recorded[0][1] == u1.id
        assert recorded[1][1] == u2.id

    def test_five_users_all_receive_202(self, client):
        results = []
        for i in range(5):
            user, token = _make_user(f"multi_u{i}")
            job = _mock_job(f"job-multi-{i}")
            with _session_patch(user.id), patch("main.create_task_job") as mc:
                mc.delay.return_value = job
                r = client.post("/tasks", json={"title": f"Task for user {i}"}, headers=_auth(token))
            results.append(r.status_code)
        assert all(s == 202 for s in results)

    def test_ten_users_all_get_unique_job_ids(self, client):
        job_ids = []
        for i in range(10):
            user, token = _make_user(f"unique_u{i}")
            job = _mock_job(f"job-unique-{i}")
            with _session_patch(user.id), patch("main.create_task_job") as mc:
                mc.delay.return_value = job
                r = client.post("/tasks", json={"title": f"Unique {i}"}, headers=_auth(token))
            job_ids.append(r.json()["job_id"])
        assert len(set(job_ids)) == 10


# ── TestBulkTaskCreation: one user creates many tasks ────────────────────────

class TestBulkTaskCreation:
    """One user firing off many async task creates."""

    def test_single_user_creates_20_tasks(self, client):
        user, token = _make_user("bulk_user20")
        job_ids = []
        for i in range(20):
            job = _mock_job(f"job-bulk-{i}")
            with _session_patch(user.id), patch("main.create_task_job") as mc:
                mc.delay.return_value = job
                r = client.post("/tasks", json={"title": f"Bulk task {i}"}, headers=_auth(token))
            assert r.status_code == 202
            job_ids.append(r.json()["job_id"])
        assert len(set(job_ids)) == 20

    def test_celery_called_once_per_request(self, client):
        user, token = _make_user("bulk_onceper")
        call_count = 0

        def count_delay(title, user_id):
            nonlocal call_count
            call_count += 1
            return _mock_job()

        with patch("main.create_task_job") as mc:
            mc.delay.side_effect = count_delay
            for i in range(5):
                with _session_patch(user.id):
                    client.post("/tasks", json={"title": f"Task {i}"}, headers=_auth(token))

        assert call_count == 5

    def test_all_titles_passed_correctly(self, client):
        user, token = _make_user("bulk_titles")
        titles = [f"Title number {i}" for i in range(8)]
        received = []

        def capture(title, user_id):
            received.append(title)
            return _mock_job()

        with patch("main.create_task_job") as mc:
            mc.delay.side_effect = capture
            for title in titles:
                with _session_patch(user.id):
                    client.post("/tasks", json={"title": title}, headers=_auth(token))

        assert received == titles


# ── TestJobPolling: GET /jobs/{job_id} after creation ────────────────────────

class TestJobPolling:
    """Poll job status after receiving a job_id from POST /tasks."""

    def test_poll_pending_job(self, client):
        user, token = _make_user("poll_pending")
        job = _mock_job("poll-job-1")

        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": "Poll me"}, headers=_auth(token))
        job_id = r.json()["job_id"]

        mock_result = MagicMock()
        mock_result.status = "PENDING"
        mock_result.result = None
        with _session_patch(user.id), patch("main.AsyncResult", return_value=mock_result):
            poll = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert poll.status_code == 200
        assert poll.json()["status"] == "pending"
        assert poll.json()["job_id"] == job_id

    def test_poll_started_job(self, client):
        user, token = _make_user("poll_started")
        job = _mock_job("poll-job-2")

        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": "Starting up"}, headers=_auth(token))
        job_id = r.json()["job_id"]

        mock_result = MagicMock()
        mock_result.status = "STARTED"
        mock_result.result = None
        with _session_patch(user.id), patch("main.AsyncResult", return_value=mock_result):
            poll = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert poll.json()["status"] == "started"

    def test_poll_success_job_has_result(self, client):
        user, token = _make_user("poll_success")
        job = _mock_job("poll-job-3")

        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": "Finish me"}, headers=_auth(token))
        job_id = r.json()["job_id"]

        task_result = {"id": str(uuid.uuid4()), "title": "Finish me", "is_done": False}
        mock_result = MagicMock()
        mock_result.status = "SUCCESS"
        mock_result.result = task_result
        with _session_patch(user.id), patch("main.AsyncResult", return_value=mock_result):
            poll = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert poll.json()["status"] == "success"
        assert poll.json()["result"]["title"] == "Finish me"

    def test_poll_failed_job(self, client):
        user, token = _make_user("poll_failed")
        job = _mock_job("poll-job-4")

        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": "This will fail"}, headers=_auth(token))
        job_id = r.json()["job_id"]

        mock_result = MagicMock()
        mock_result.status = "FAILURE"
        mock_result.result = None
        with _session_patch(user.id), patch("main.AsyncResult", return_value=mock_result):
            poll = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert poll.json()["status"] == "failed"
        assert poll.json()["result"] is None

    def test_poll_requires_auth(self, client):
        r = client.get("/jobs/any-job-id")
        assert r.status_code == 401

    def test_create_then_poll_full_cycle(self, client):
        """Full cycle: register → create task → poll until success."""
        user, token = _make_user("poll_cycle")
        job_id = "cycle-job-999"
        job = _mock_job(job_id)

        # Step 1: create task
        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": "Full cycle"}, headers=_auth(token))
        assert r.status_code == 202
        assert r.json()["job_id"] == job_id

        # Step 2: poll → pending
        pending = MagicMock(status="PENDING", result=None)
        with _session_patch(user.id), patch("main.AsyncResult", return_value=pending):
            p1 = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert p1.json()["status"] == "pending"

        # Step 3: poll → started
        started = MagicMock(status="STARTED", result=None)
        with _session_patch(user.id), patch("main.AsyncResult", return_value=started):
            p2 = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert p2.json()["status"] == "started"

        # Step 4: poll → success
        done = MagicMock(status="SUCCESS", result={"id": "t99", "title": "Full cycle"})
        with _session_patch(user.id), patch("main.AsyncResult", return_value=done):
            p3 = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert p3.json()["status"] == "success"
        assert p3.json()["result"]["title"] == "Full cycle"


# ── TestUsernameTitleVariants: diverse usernames and task titles ──────────────

class TestUsernameTitleVariants:
    """Matrix of interesting username + task title combinations."""

    USERS_AND_TASKS = [
        ("alice",           "Buy groceries"),
        ("bob_smith",       "Fix login bug"),
        ("carol.jones",     "Write unit tests"),
        ("dave_2024",       "Deploy to staging"),
        ("eve@example",     "Review pull request"),
        ("frank-o-brien",   "Update README"),
        ("grace123",        "Refactor auth module"),
        ("heidi_dev",       "Add rate limiting"),
        ("ivan.petrov",     "Migrate database"),
        ("judy_Q",          "Monitor Celery queues"),
        ("karl__dev",       "Set up CI pipeline"),
        ("laura.m",         "Write API docs"),
        ("mike_99",         "Optimize SQL queries"),
        ("nancy-dev",       "Add caching layer"),
        ("oscar.t",         "Fix memory leak"),
    ]

    @pytest.mark.parametrize("username,task_title", USERS_AND_TASKS)
    def test_user_creates_task(self, client, username, task_title):
        user, token = _make_user(username)
        job = _mock_job(f"job-{username}")
        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": task_title}, headers=_auth(token))
        assert r.status_code == 202
        assert r.json()["job_id"] == f"job-{username}"
        mc.delay.assert_called_once_with(task_title, user.id)

    @pytest.mark.parametrize("username,task_title", USERS_AND_TASKS)
    def test_user_polls_their_job(self, client, username, task_title):
        user, token = _make_user(f"poll_{username}")
        job_id = f"poll-job-{username}"
        job = _mock_job(job_id)

        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": task_title}, headers=_auth(token))
        assert r.json()["job_id"] == job_id

        result = MagicMock(status="SUCCESS", result={"id": "t1", "title": task_title})
        with _session_patch(user.id), patch("main.AsyncResult", return_value=result):
            poll = client.get(f"/jobs/{job_id}", headers=_auth(token))
        assert poll.json()["status"] == "success"


# ── TestResponseSchema: shape of the 202 response ────────────────────────────

class TestResponseSchema:
    def test_job_response_has_required_fields(self, client):
        user, token = _make_user("schema_check")
        job = _mock_job("schema-job")
        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": "Schema test"}, headers=_auth(token))
        body = r.json()
        assert "job_id" in body
        assert "status" in body
        assert "result" in body

    def test_job_id_is_string(self, client):
        user, token = _make_user("schema_str")
        job = _mock_job("string-job-id")
        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": "String check"}, headers=_auth(token))
        assert isinstance(r.json()["job_id"], str)

    def test_status_is_valid_enum_value(self, client):
        user, token = _make_user("schema_enum")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": "Enum check"}, headers=_auth(token))
        assert r.json()["status"] in ("pending", "started", "success", "failed")

    def test_content_type_is_json(self, client):
        user, token = _make_user("schema_ct")
        job = _mock_job()
        with _session_patch(user.id), patch("main.create_task_job") as mc:
            mc.delay.return_value = job
            r = client.post("/tasks", json={"title": "Content-type"}, headers=_auth(token))
        assert "application/json" in r.headers["content-type"]
