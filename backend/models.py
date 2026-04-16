import uuid
from sqlalchemy import Boolean, Column, DateTime, String, func, Index
from database import Base


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String, nullable=False)
    is_done = Column(Boolean, default=False, nullable=False)

    # Timestamps are essential in production for auditing, sorting, and
    # debugging. Add them now — retrofitting onto existing data is painful.
    # server_default uses the DB clock so app-server timezone mismatches
    # don't corrupt the values.
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # onupdate is set by SQLAlchemy on every UPDATE statement automatically.
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    # Index is_done so filtering incomplete tasks (the most common query)
    # doesn't full-scan the table as it grows.
    __table_args__ = (
        Index("ix_tasks_is_done", "is_done"),
    )
