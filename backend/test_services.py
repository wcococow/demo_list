"""
Unit and integration tests for the service layer.
Uses in-memory SQLite — no running DB or Redis required.
"""
import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import User, Task
import task_service
import user_service


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def alice(db):
    user = User(username="alice", hashed_password="hashed")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def bob(db):
    user = User(username="bob", hashed_password="hashed")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── task_service ──────────────────────────────────────────────────────────────

class TestCreateTask:
    def test_creates_task(self, db, alice):
        task = task_service.create_task(db, "Buy milk", alice.id)
        assert task.id is not None
        assert task.title == "Buy milk"
        assert task.is_done is False
        assert task.owner_id == alice.id

    def test_task_persisted(self, db, alice):
        task = task_service.create_task(db, "Buy milk", alice.id)
        fetched = db.query(Task).filter(Task.id == task.id).first()
        assert fetched is not None
        assert fetched.title == "Buy milk"


class TestGetAllTasks:
    def test_returns_only_owner_tasks(self, db, alice, bob):
        task_service.create_task(db, "Alice task", alice.id)
        task_service.create_task(db, "Bob task", bob.id)

        alice_tasks = task_service.get_all_tasks(db, alice.id)
        assert len(alice_tasks) == 1
        assert alice_tasks[0].title == "Alice task"

    def test_pagination(self, db, alice):
        for i in range(5):
            task_service.create_task(db, f"Task {i}", alice.id)

        page = task_service.get_all_tasks(db, alice.id, skip=2, limit=2)
        assert len(page) == 2


class TestUpdateTask:
    def test_updates_title(self, db, alice):
        task = task_service.create_task(db, "Old title", alice.id)
        updated = task_service.update_task(db, task.id, alice.id, title="New title")
        assert updated.title == "New title"

    def test_updates_is_done(self, db, alice):
        task = task_service.create_task(db, "Task", alice.id)
        updated = task_service.update_task(db, task.id, alice.id, is_done=True)
        assert updated.is_done is True

    def test_wrong_owner_returns_none(self, db, alice, bob):
        task = task_service.create_task(db, "Alice task", alice.id)
        result = task_service.update_task(db, task.id, bob.id, title="Hacked")
        assert result is None

    def test_bad_id_returns_none(self, db, alice):
        result = task_service.update_task(db, "nonexistent", alice.id, title="X")
        assert result is None


class TestDeleteTask:
    def test_deletes_own_task(self, db, alice):
        task = task_service.create_task(db, "Task", alice.id)
        result = task_service.delete_task(db, task.id, alice.id)
        assert result is True
        assert db.query(Task).filter(Task.id == task.id).first() is None

    def test_wrong_owner_returns_false(self, db, alice, bob):
        task = task_service.create_task(db, "Alice task", alice.id)
        result = task_service.delete_task(db, task.id, bob.id)
        assert result is False
        assert db.query(Task).filter(Task.id == task.id).first() is not None

    def test_bad_id_returns_false(self, db, alice):
        assert task_service.delete_task(db, "nonexistent", alice.id) is False


# ── user_service ──────────────────────────────────────────────────────────────

class TestCreateUser:
    def test_creates_user(self, db):
        with patch("user_service.create_session"), \
             patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.clear_failed_logins"):
            user = user_service.create_user(db, "charlie", "password123")
            assert user.id is not None
            assert user.username == "charlie"

    def test_password_is_hashed(self, db):
        with patch("user_service.create_session"), \
             patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.clear_failed_logins"):
            user = user_service.create_user(db, "charlie", "password123")
            assert user.hashed_password != "password123"


class TestAuthenticateUser:
    def test_wrong_password_returns_none(self, db, alice):
        with patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.verify_password", return_value=False), \
             patch("user_service.record_failed_login") as mock_fail:
            result = user_service.authenticate_user(db, "alice", "wrongpass")
            assert result is None
            mock_fail.assert_called_once_with("alice")

    def test_unknown_user_returns_none(self, db):
        with patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.record_failed_login") as mock_fail:
            result = user_service.authenticate_user(db, "nobody", "pass")
            assert result is None
            mock_fail.assert_called_once_with("nobody")

    def test_locked_account_raises(self, db, alice):
        with patch("user_service.is_account_locked", return_value=True):
            with pytest.raises(ValueError, match="Account locked"):
                user_service.authenticate_user(db, "alice", "secret")

    def test_success_clears_failed_logins(self, db):
        with patch("user_service.is_account_locked", return_value=False), \
             patch("user_service.clear_failed_logins") as mock_clear, \
             patch("user_service.create_session", return_value="session-id"), \
             patch("user_service.create_access_token", return_value="token"):
            # create user with real hashed password
            user = user_service.create_user(db, "dave", "correct")
            result = user_service.authenticate_user(db, "dave", "correct")
            assert result == "token"
            mock_clear.assert_called_once_with("dave")


# ── session_manager ───────────────────────────────────────────────────────────

class TestSessionManager:
    def test_create_and_get_session(self):
        import session_manager
        mock_redis = MagicMock()
        mock_redis.get.return_value = "user-123"
        with patch("session_manager.redis_client", mock_redis):
            session_id = session_manager.create_session("user-123")
            assert session_id is not None
            mock_redis.setex.assert_called_once()

    def test_invalidate_session(self):
        import session_manager
        mock_redis = MagicMock()
        mock_redis.delete.return_value = 1
        with patch("session_manager.redis_client", mock_redis):
            session_manager.invalidate_session("some-session-id")
            mock_redis.delete.assert_called_once()

    def test_is_account_locked(self):
        import session_manager
        mock_redis = MagicMock()
        mock_redis.exists.return_value = 1
        with patch("session_manager.redis_client", mock_redis):
            assert session_manager.is_account_locked("alice") is True

    def test_record_failed_login_triggers_lockout(self):
        import session_manager
        mock_redis = MagicMock()
        mock_redis.incr.return_value = 5  # 5th failure = lockout
        with patch("session_manager.redis_client", mock_redis):
            session_manager.record_failed_login("alice")
            mock_redis.setex.assert_called_once()  # lockout key set
