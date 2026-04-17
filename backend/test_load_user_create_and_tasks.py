"""
Load test: massive concurrent user registrations + task creation bursts.

Designed to generate observable spikes in Grafana dashboards and k8s metrics:
  - CPU / memory pressure from concurrent bcrypt hashing (user register)
  - High request rate on POST /tasks (Celery job enqueue throughput)
  - Error-rate panel behaviour under auth/validation failures
  - Rate-limiter panel (429s from the 60/min task limit per IP)

Run against a live stack:
    BASE_URL=http://localhost:8000 pytest test_load_user_create_and_tasks.py -v -s

Environment variables (all optional):
    BASE_URL        default: http://localhost:8000
    WAVE_USERS      users per registration wave   (default: 50)
    TASKS_PER_USER  tasks each user creates        (default: 20)
    TASK_WAVES      number of back-to-back task waves (default: 3)
    CONCURRENCY     max threads in the thread pool  (default: 30)
"""

import os
import time
import uuid
import threading
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import pytest
import requests

# ── config ────────────────────────────────────────────────────────────────────

BASE_URL        = os.getenv("BASE_URL",       "http://localhost:8000")
WAVE_USERS      = int(os.getenv("WAVE_USERS",      "50"))
TASKS_PER_USER  = int(os.getenv("TASKS_PER_USER",  "20"))
TASK_WAVES      = int(os.getenv("TASK_WAVES",       "3"))
CONCURRENCY     = int(os.getenv("CONCURRENCY",     "30"))
REQUEST_TIMEOUT = 15  # seconds per HTTP call

# ── shared state ──────────────────────────────────────────────────────────────

_lock = threading.Lock()


@dataclass
class Stats:
    successes: int = 0
    failures:  int = 0
    latencies: list = field(default_factory=list)

    def record(self, ok: bool, latency_ms: float):
        with _lock:
            if ok:
                self.successes += 1
            else:
                self.failures += 1
            self.latencies.append(latency_ms)

    def summary(self) -> dict:
        lats = self.latencies or [0]
        return {
            "total":      self.successes + self.failures,
            "successes":  self.successes,
            "failures":   self.failures,
            "p50_ms":     round(statistics.median(lats), 1),
            "p95_ms":     round(statistics.quantiles(lats, n=20)[18], 1) if len(lats) >= 20 else max(lats),
            "max_ms":     round(max(lats), 1),
        }


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _register(username: str, password: str) -> Optional[dict]:
    t0 = time.monotonic()
    try:
        r = requests.post(
            f"{BASE_URL}/auth/register",
            json={"username": username, "password": password},
            timeout=REQUEST_TIMEOUT,
        )
        latency = (time.monotonic() - t0) * 1000
        if r.status_code == 201:
            return {"user": r.json(), "latency_ms": latency}
        return {"error": r.status_code, "latency_ms": latency}
    except requests.RequestException as exc:
        return {"error": str(exc), "latency_ms": (time.monotonic() - t0) * 1000}


def _login(username: str, password: str) -> Optional[str]:
    try:
        r = requests.post(
            f"{BASE_URL}/auth/login",
            data={"username": username, "password": password},
            timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()["access_token"]
    except requests.RequestException:
        pass
    return None


def _create_task(token: str, title: str) -> dict:
    t0 = time.monotonic()
    try:
        r = requests.post(
            f"{BASE_URL}/tasks",
            json={"title": title},
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
        latency = (time.monotonic() - t0) * 1000
        return {"status_code": r.status_code, "body": r.json(), "latency_ms": latency}
    except requests.RequestException as exc:
        return {"status_code": 0, "error": str(exc), "latency_ms": (time.monotonic() - t0) * 1000}


def _print_stats(label: str, stats: Stats):
    s = stats.summary()
    print(
        f"\n[{label}] total={s['total']}  ok={s['successes']}  err={s['failures']}  "
        f"p50={s['p50_ms']}ms  p95={s['p95_ms']}ms  max={s['max_ms']}ms"
    )


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def registered_users():
    """
    Register WAVE_USERS users concurrently and return a list of
    (username, token) pairs for use in subsequent tests.
    """
    password = "LoadTest!99"
    usernames = [f"lt_user_{uuid.uuid4().hex[:10]}" for _ in range(WAVE_USERS)]
    reg_stats = Stats()
    tokens: list[tuple[str, str]] = []

    def register_and_login(username):
        result = _register(username, password)
        ok = isinstance(result, dict) and "user" in result
        reg_stats.record(ok, result.get("latency_ms", 0) if result else 0)
        if ok:
            token = _login(username, password)
            if token:
                return (username, token)
        return None

    print(f"\n\n{'='*60}")
    print(f"WAVE 1 — registering {WAVE_USERS} users (concurrency={CONCURRENCY})")
    print(f"{'='*60}")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(register_and_login, u) for u in usernames]
        for fut in as_completed(futures):
            res = fut.result()
            if res:
                tokens.append(res)

    _print_stats("user_register", reg_stats)
    assert len(tokens) > 0, "No users successfully registered — is the server running?"
    return tokens


# ── Test 1: Registration wave ─────────────────────────────────────────────────

class TestUserRegistrationWave:
    """
    Fires WAVE_USERS concurrent POST /auth/register requests.
    Expected Grafana signals:
      - request_rate spike on /auth/register
      - CPU spike (bcrypt is compute-intensive)
      - 201 success rate ~100 %
    """

    def test_all_users_registered(self, registered_users):
        assert len(registered_users) >= int(WAVE_USERS * 0.9), (
            f"Expected ≥90% registration success, got {len(registered_users)}/{WAVE_USERS}"
        )

    def test_unique_usernames(self, registered_users):
        names = [u for u, _ in registered_users]
        assert len(set(names)) == len(names), "Duplicate usernames detected"

    def test_all_tokens_non_empty(self, registered_users):
        for username, token in registered_users:
            assert token and len(token) > 20, f"Bad token for {username}"


# ── Test 2: Duplicate registration (error rate panel) ────────────────────────

class TestDuplicateRegistrationErrors:
    """
    Re-registers the first 10 users → 409 Conflict.
    Expected Grafana signals:
      - error_rate panel shows 4xx bump
    """

    def test_duplicate_returns_409(self, registered_users):
        password = "LoadTest!99"
        targets = registered_users[:10]
        stats = Stats()

        def attempt_duplicate(username):
            result = _register(username, password)
            code = result.get("error") if result else 0
            stats.record(code == 409, result.get("latency_ms", 0) if result else 0)
            return code

        with ThreadPoolExecutor(max_workers=10) as pool:
            codes = list(pool.map(lambda u_t: attempt_duplicate(u_t[0]), targets))

        _print_stats("duplicate_register", stats)
        assert all(c == 409 for c in codes), f"Expected all 409, got: {set(codes)}"


# ── Test 3: Massive task creation — single wave ───────────────────────────────

class TestMassiveTaskCreationSingleWave:
    """
    Every registered user creates TASKS_PER_USER tasks in parallel.
    Total requests = WAVE_USERS × TASKS_PER_USER.
    Expected Grafana signals:
      - POST /tasks request rate spike
      - Celery task queue depth spike
      - 202 acceptance rate ~100 %
    """

    def test_bulk_task_create_202(self, registered_users):
        task_stats = Stats()
        job_ids: list[str] = []

        def create_tasks_for_user(username_token):
            username, token = username_token
            local_ids = []
            for i in range(TASKS_PER_USER):
                res = _create_task(token, f"{username} task #{i+1}")
                ok = res["status_code"] == 202
                task_stats.record(ok, res["latency_ms"])
                if ok:
                    local_ids.append(res["body"].get("job_id"))
            return local_ids

        print(f"\n\n{'='*60}")
        print(
            f"WAVE 2 — {len(registered_users)} users × {TASKS_PER_USER} tasks "
            f"= {len(registered_users) * TASKS_PER_USER} total task requests"
        )
        print(f"{'='*60}")

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for ids in pool.map(create_tasks_for_user, registered_users):
                job_ids.extend(ids)

        _print_stats("task_create_wave1", task_stats)

        total_expected = len(registered_users) * TASKS_PER_USER
        assert task_stats.successes >= int(total_expected * 0.9), (
            f"Expected ≥90% task acceptance: {task_stats.successes}/{total_expected}"
        )
        # All returned job_ids should be unique strings
        valid_ids = [j for j in job_ids if j]
        assert len(set(valid_ids)) == len(valid_ids), "Duplicate job IDs returned"


# ── Test 4: Repeated task waves (sustained load) ──────────────────────────────

class TestSustainedTaskWaves:
    """
    Repeats task-creation waves TASK_WAVES times with a short pause between them.
    Expected Grafana signals:
      - Repeated request rate spikes separated by a cooldown valley
      - Celery worker utilisation over time
      - k8s HPA trigger (if autoscaling is enabled)
    """

    def test_repeated_waves(self, registered_users):
        all_wave_stats: list[Stats] = []

        for wave in range(1, TASK_WAVES + 1):
            wave_stats = Stats()

            def create_one_task(username_token, wave_num=wave):
                username, token = username_token
                title = f"Wave {wave_num} — {username} — {uuid.uuid4().hex[:6]}"
                res = _create_task(token, title)
                ok = res["status_code"] == 202
                wave_stats.record(ok, res["latency_ms"])

            print(f"\n{'='*60}")
            print(f"WAVE {wave + 2} — sustained task wave {wave}/{TASK_WAVES}")
            print(f"{'='*60}")

            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                list(pool.map(create_one_task, registered_users))

            _print_stats(f"task_wave_{wave}", wave_stats)
            all_wave_stats.append(wave_stats)

            if wave < TASK_WAVES:
                print(f"  sleeping 2 s between waves…")
                time.sleep(2)

        for i, stats in enumerate(all_wave_stats, 1):
            assert stats.successes > 0, f"Wave {i}: zero successful task creates"


# ── Test 5: Unauthenticated flood (error-rate panel) ─────────────────────────

class TestUnauthenticatedTaskFlood:
    """
    Fires 100 task-create requests with no/bad tokens.
    Expected Grafana signals:
      - 401 error rate bump on POST /tasks
    """

    def test_no_token_returns_401(self):
        stats = Stats()

        def bad_request(_):
            t0 = time.monotonic()
            try:
                r = requests.post(
                    f"{BASE_URL}/tasks",
                    json={"title": "should fail"},
                    timeout=REQUEST_TIMEOUT,
                )
                latency = (time.monotonic() - t0) * 1000
                ok = r.status_code == 401
                stats.record(ok, latency)
                return r.status_code
            except requests.RequestException as exc:
                stats.record(False, (time.monotonic() - t0) * 1000)
                return str(exc)

        print(f"\n\n{'='*60}")
        print("WAVE — unauthenticated task flood (100 requests)")
        print(f"{'='*60}")

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            codes = list(pool.map(bad_request, range(100)))

        _print_stats("unauth_flood", stats)
        assert stats.successes >= 90, f"Expected ≥90 × 401, got {stats.successes}/100"


# ── Test 6: Invalid-payload flood (validation error panel) ───────────────────

class TestInvalidPayloadFlood:
    """
    Fires malformed task-create requests from legitimate users.
    Expected Grafana signals:
      - 422 error rate bump
    """

    INVALID_PAYLOADS = [
        {},
        {"title": ""},
        {"title": "   "},
        {"title": None},
        {"title": "x" * 300},
        {"bad_field": "whatever"},
    ]

    def test_invalid_payloads_return_422(self, registered_users):
        stats = Stats()
        sample = registered_users[:10]

        def send_bad(args):
            username_token, payload = args
            _, token = username_token
            t0 = time.monotonic()
            try:
                r = requests.post(
                    f"{BASE_URL}/tasks",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=REQUEST_TIMEOUT,
                )
                latency = (time.monotonic() - t0) * 1000
                ok = r.status_code == 422
                stats.record(ok, latency)
                return r.status_code
            except requests.RequestException as exc:
                stats.record(False, (time.monotonic() - t0) * 1000)
                return str(exc)

        import itertools
        combos = list(itertools.product(sample, self.INVALID_PAYLOADS))

        print(f"\n\n{'='*60}")
        print(f"WAVE — invalid payload flood ({len(combos)} requests)")
        print(f"{'='*60}")

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            codes = list(pool.map(send_bad, combos))

        _print_stats("invalid_payload", stats)
        assert stats.successes >= int(len(combos) * 0.9), (
            f"Expected ≥90% 422 responses, got {stats.successes}/{len(combos)}"
        )
