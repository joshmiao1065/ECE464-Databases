import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class CollectionCreate(BaseModel):
    name: str
    description: Optional[str] = None
    is_private: bool = False


class CollectionOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: Optional[str]
    is_private: bool
    created_at: datetime

    model_config = {"from_attributes": True}
