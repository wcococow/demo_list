from datetime import datetime
from enum import Enum
from typing import Annotated, Any
from pydantic import BaseModel, ConfigDict, StringConstraints

TrimmedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class UserCreate(BaseModel):
    username: TrimmedStr
    password: Annotated[str, StringConstraints(min_length=6, max_length=100)]


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TaskCreate(BaseModel):
    title: TrimmedStr


class TaskUpdate(BaseModel):
    title: TrimmedStr | None = None
    is_done: bool | None = None


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    is_done: bool
    owner_id: str
    created_at: datetime
    updated_at: datetime


class JobStatus(str, Enum):
    pending = "pending"
    started = "started"
    success = "success"
    failed  = "failed"


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    result: Any | None = None


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
