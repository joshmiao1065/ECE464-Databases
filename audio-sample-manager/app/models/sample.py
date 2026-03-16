import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    freesound_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, unique=True, index=True
    )
    file_url: Mapped[str] = mapped_column(String(512), nullable=False)
    waveform_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, server_default="'audio/mpeg'"
    )
    user_id_owner: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    pack_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("packs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # Relationships
    owner = relationship("User", back_populates="samples", foreign_keys=[user_id_owner])
    pack = relationship("Pack", back_populates="samples", foreign_keys=[pack_id])
    embedding = relationship("AudioEmbedding", back_populates="sample", uselist=False)
    audio_metadata = relationship("AudioMetadata", back_populates="sample", uselist=False)
    tags = relationship("Tag", secondary="sample_tags", back_populates="samples")
    comments = relationship("Comment", back_populates="sample")
    ratings = relationship("Rating", back_populates="sample")
    download_history = relationship("DownloadHistory", back_populates="sample")
    processing_queue = relationship("ProcessingQueue", back_populates="sample")
    collections = relationship("Collection", secondary="collection_items", back_populates="samples")
