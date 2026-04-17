from celery_app import celery_app
from database import SessionLocal
import task_service


def _task_to_dict(task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "is_done": task.is_done,
        "owner_id": task.owner_id,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


@celery_app.task(bind=True, name="tasks.create", max_retries=3)
def create_task_job(self, title: str, owner_id: str):
    db = SessionLocal()
    try:
        task = task_service.create_task(db, title, owner_id)
        return _task_to_dict(task)
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
    finally:
        db.close()


@celery_app.task(bind=True, name="tasks.update", max_retries=3)
def update_task_job(self, task_id: str, owner_id: str, title=None, is_done=None):
    db = SessionLocal()
    try:
        task = task_service.update_task(db, task_id, owner_id, title=title, is_done=is_done)
        return _task_to_dict(task) if task else None
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
    finally:
        db.close()


@celery_app.task(bind=True, name="tasks.delete", max_retries=3)
def delete_task_job(self, task_id: str, owner_id: str):
    db = SessionLocal()
    try:
        return task_service.delete_task(db, task_id, owner_id)
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
    finally:
        db.close()
