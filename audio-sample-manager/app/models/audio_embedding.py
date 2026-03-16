import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, DateTime, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from pgvector.sqlalchemy import Vector

from .base import Base


class AudioEmbedding(Base):
    __tablename__ = "audio_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    sample_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("samples.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # 512-dim CLAP embedding; HNSW index on this column is created in the migration
    embedding: Mapped[list] = mapped_column(Vector(512), nullable=False)
    model_version: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, server_default="'clap-htsat-fused'"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    sample = relationship("Sample", back_populates="embedding")
