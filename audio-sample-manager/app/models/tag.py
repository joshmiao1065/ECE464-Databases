import uuid
from typing import Optional

from sqlalchemy import String, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from .base import Base


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)

    samples = relationship("Sample", secondary="sample_tags", back_populates="tags")


class SampleTag(Base):
    __tablename__ = "sample_tags"

    sample_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("samples.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    # 'auto' = applied by YAMNet/CLAP pipeline; 'manual' = applied by user
    source: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, server_default="'auto'"
    )
