"""
Global pytest configuration.
Sets required environment variables and provides session-scoped mocks for
external services (Redis, OTel) so both test_services.py and test_main.py
can run without a live Redis or Jaeger instance.
"""
import os
import pytest
from unittest.mock import MagicMock, patch

# Must be set before any module that imports auth.py is loaded.
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")


@pytest.fixture(scope="session", autouse=True)
def mock_redis_globally():
    """Replace the Redis client used everywhere with a MagicMock for the whole session."""
    mock = MagicMock()
    # get_session_user_id must return a truthy value so auth middleware passes
    mock.get.return_value = None  # default: no active session
    with patch("redis_client.redis_client", mock), \
         patch("session_manager.redis_client", mock):
        yield mock


@pytest.fixture(scope="session", autouse=True)
def mock_telemetry_globally():
    """Prevent setup_telemetry from touching OTel/gRPC during tests."""
    with patch("telemetry.setup_telemetry"):
        yield
