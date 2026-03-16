import uuid
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel


class AudioMetadataOut(BaseModel):
    bpm: Optional[float] = None
    key: Optional[str] = None
    energy_level: Optional[float] = None
    loudness_lufs: Optional[float] = None
    spectral_centroid: Optional[float] = None
    zero_crossing_rate: Optional[float] = None
    is_processed: bool

    model_config = {"from_attributes": True}


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    category: Optional[str] = None

    model_config = {"from_attributes": True}


class SampleOut(BaseModel):
    id: uuid.UUID
    title: str
    freesound_id: Optional[int] = None
    file_url: str
    waveform_url: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime
    audio_metadata: Optional[AudioMetadataOut] = None
    tags: List[TagOut] = []

    model_config = {"from_attributes": True}


class SampleCreate(BaseModel):
    title: str
    freesound_id: Optional[int] = None
    file_url: str
    waveform_url: Optional[str] = None
    duration_ms: Optional[int] = None
    file_size_bytes: Optional[int] = None
    mime_type: Optional[str] = "audio/mpeg"
