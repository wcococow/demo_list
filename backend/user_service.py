from sqlalchemy.orm import Session

from auth import hash_password, verify_password, create_access_token
from models import User
from session_manager import (
    create_session, is_account_locked,
    record_failed_login, clear_failed_logins,
)
from telemetry import tracer


@tracer.start_as_current_span("user_service.get_by_username")
def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(User.username == username).first()


@tracer.start_as_current_span("user_service.create")
def create_user(db: Session, username: str, password: str) -> User:
    user = User(username=username, hashed_password=hash_password(password))
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()
        raise
    return user


@tracer.start_as_current_span("user_service.authenticate")
def authenticate_user(db: Session, username: str, password: str) -> str | None:
    """
    Returns JWT on success.
    Returns None on bad credentials.
    Raises ValueError if account is locked.
    """
    if is_account_locked(username):
        raise ValueError("Account locked — too many failed attempts. Try again in 15 minutes.")

    user = get_user_by_username(db, username)
    if not user or not verify_password(password, user.hashed_password):
        record_failed_login(username)
        return None

    clear_failed_logins(username)
    session_id = create_session(user.id)
    return create_access_token(user.id, session_id)
