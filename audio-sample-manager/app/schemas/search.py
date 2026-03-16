from typing import List, Optional

from pydantic import BaseModel, Field

from .sample import SampleOut


class TextSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class SearchResponse(BaseModel):
    results: List[SampleOut]
    total: int
    query: Optional[str] = None
