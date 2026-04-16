from sqlalchemy.orm import Session
from models import Task


def create_task(db: Session, title: str):
    task = Task(title=title)
    db.add(task)
    # FIX #7: wrap commit in try/except so a DB error (e.g. constraint violation)
    # triggers a rollback instead of leaving the session in a broken state,
    # which would poison every subsequent request on the same connection
    try:
        db.commit()
        db.refresh(task)
    except Exception:
        db.rollback()
        raise
    return task


# FIX #5: added skip/limit so callers can paginate instead of loading every row
def get_all_tasks(db: Session, skip: int = 0, limit: int = 20):
    return db.query(Task).offset(skip).limit(limit).all()


def get_task_by_id(db: Session, task_id: str):
    return db.query(Task).filter(Task.id == task_id).first()


def update_task(db: Session, task_id: str, title=None, is_done=None):
    task = get_task_by_id(db, task_id)

    if not task:
        return None

    if title is not None:
        task.title = title

    if is_done is not None:
        task.is_done = is_done

    # FIX #7: rollback on commit failure to keep the session clean
    try:
        db.commit()
        db.refresh(task)
    except Exception:
        db.rollback()
        raise
    return task


def delete_task(db: Session, task_id: str):
    task = get_task_by_id(db, task_id)

    if not task:
        return False

    db.delete(task)
    # FIX #7: rollback on commit failure to keep the session clean
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True
