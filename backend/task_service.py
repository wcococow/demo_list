from sqlalchemy.orm import Session

from models import Task
from telemetry import tracer


@tracer.start_as_current_span("task_service.create")
def create_task(db: Session, title: str, owner_id: str):
    task = Task(title=title, owner_id=owner_id)
    db.add(task)
    try:
        db.commit()
        db.refresh(task)
    except Exception:
        db.rollback()
        raise
    return task


@tracer.start_as_current_span("task_service.get_all")
def get_all_tasks(db: Session, owner_id: str, skip: int = 0, limit: int = 20):
    return db.query(Task).filter(Task.owner_id == owner_id).offset(skip).limit(limit).all()


@tracer.start_as_current_span("task_service.get_one")
def get_task_by_id(db: Session, task_id: str):
    return db.query(Task).filter(Task.id == task_id).first()


@tracer.start_as_current_span("task_service.update")
def update_task(db: Session, task_id: str, owner_id: str, title=None, is_done=None):
    task = db.query(Task).filter(Task.id == task_id, Task.owner_id == owner_id).first()

    if not task:
        return None

    if title is not None:
        task.title = title

    if is_done is not None:
        task.is_done = is_done

    try:
        db.commit()
        db.refresh(task)
    except Exception:
        db.rollback()
        raise
    return task


@tracer.start_as_current_span("task_service.delete")
def delete_task(db: Session, task_id: str, owner_id: str):
    task = db.query(Task).filter(Task.id == task_id, Task.owner_id == owner_id).first()

    if not task:
        return False

    db.delete(task)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True
