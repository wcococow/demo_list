import os
import json
import logging
from celery import Celery
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

_base_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
_broker   = _base_url.rstrip("/0") + "/1"

celery_app = Celery(
    "task_api",
    broker=_broker,
    backend=_broker,
    include=["task_jobs"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    result_expires=3600,
    worker_prefetch_multiplier=4,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Route exhausted tasks to dead letter queue instead of dropping them
    task_routes={
        "tasks.*": {"queue": "celery"},
    },
)


class BaseTaskWithDLQ(celery_app.Task):
    """Base task — pushes to dead_letter Redis list after all retries exhausted."""
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        from redis_client import redis_client
        entry = json.dumps({
            "task_id": task_id,
            "task_name": self.name,
            "args": args,
            "kwargs": kwargs,
            "error": str(exc),
        })
        redis_client.lpush("dead_letter", entry)
        redis_client.ltrim("dead_letter", 0, 999)  # keep last 1000 failures
        logger.error("Task %s moved to dead letter queue: %s", task_id, exc)


celery_app.Task = BaseTaskWithDLQ
