import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CommentCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class CommentOut(BaseModel):
    id: uuid.UUID
    user_id: Optional[uuid.UUID]
    username: Optional[str]      # denormalised from the users table for display
    sample_id: uuid.UUID
    text: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RatingCreate(BaseModel):
    score: int = Field(..., ge=1, le=5)


class RatingOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    sample_id: uuid.UUID
    score: int
    created_at: datetime

    model_config = {"from_attributes": True}


class RatingStats(BaseModel):
    average: Optional[float]   # None when no ratings yet
    count: int


class DownloadStats(BaseModel):
    total: int                       # total downloads across all users
    user_downloads: Optional[int]    # current user's count; None if not authenticated
