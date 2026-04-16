from datetime import datetime
from typing import Annotated
from pydantic import BaseModel, ConfigDict, StringConstraints

# Reusable validated string type — strips whitespace so "  " can't sneak
# through as a valid title, and enforces a sensible length bound.
TrimmedStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


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
    created_at: datetime
    updated_at: datetime


# Consistent error envelope — all error responses have the same shape so API
# consumers don't need to handle two different error formats (HTTPException
# returns {"detail": "..."} while validation errors return a nested structure).
class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
