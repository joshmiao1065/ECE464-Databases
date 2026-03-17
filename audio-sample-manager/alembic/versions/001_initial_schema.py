"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-03-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from pgvector.sqlalchemy import Vector

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # Extensions
    # -------------------------------------------------------------------------
    op.execute('CREATE EXTENSION IF NOT EXISTS vector')
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # -------------------------------------------------------------------------
    # Enums
    # -------------------------------------------------------------------------
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE processing_status AS ENUM ('pending', 'processing', 'done', 'failed');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE query_type AS ENUM ('text', 'audio');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    # -------------------------------------------------------------------------
    # users
    # -------------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("username", sa.String(50), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("preferences_json", sa.JSON, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )

    # -------------------------------------------------------------------------
    # packs  (Freesound packs OR user-curated packs)
    # -------------------------------------------------------------------------
    op.create_table(
        "packs",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("source_url", sa.String(512), nullable=True),
        # NULL for user-created packs; non-NULL for packs ingested from Freesound
        sa.Column("freesound_pack_id", sa.Integer, nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )

    # -------------------------------------------------------------------------
    # samples
    # -------------------------------------------------------------------------
    op.create_table(
        "samples",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("freesound_id", sa.Integer, nullable=True, unique=True),
        sa.Column("file_url", sa.String(512), nullable=False),
        sa.Column("waveform_url", sa.String(512), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("file_size_bytes", sa.Integer, nullable=True),
        sa.Column("mime_type", sa.String(50), nullable=True, server_default="'audio/mpeg'"),
        sa.Column("user_id_owner", sa.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        # Original Freesound pack; many-to-many via pack_samples for curated packs
        sa.Column("pack_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("packs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )
    op.create_index("ix_samples_freesound_id", "samples", ["freesound_id"])
    op.create_index("ix_samples_user_id_owner", "samples", ["user_id_owner"])
    op.create_index("ix_samples_pack_id", "samples", ["pack_id"])

    # -------------------------------------------------------------------------
    # audio_embeddings  (1:1 with samples; kept separate for cleaner ALTER TABLE)
    # -------------------------------------------------------------------------
    op.create_table(
        "audio_embeddings",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("sample_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("samples.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("embedding", Vector(512), nullable=False),
        sa.Column("model_version", sa.String(50), nullable=True,
                  server_default="'clap-htsat-fused'"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )
    # HNSW index — fast approximate cosine search; tune m/ef_construction for recall vs speed
    op.execute("""
        CREATE INDEX ix_audio_embeddings_hnsw
        ON audio_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # -------------------------------------------------------------------------
    # audio_metadata  (1:1 with samples; populated by Librosa worker)
    # -------------------------------------------------------------------------
    op.create_table(
        "audio_metadata",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("sample_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("samples.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("bpm", sa.Float, nullable=True),
        sa.Column("key", sa.String(4), nullable=True),           # e.g. "C#", "Gmaj" (root only for now)
        sa.Column("energy_level", sa.Float, nullable=True),
        sa.Column("loudness_lufs", sa.Float, nullable=True),
        sa.Column("spectral_centroid", sa.Float, nullable=True),
        sa.Column("zero_crossing_rate", sa.Float, nullable=True),
        sa.Column("sample_rate", sa.Integer, nullable=True),
        # Quick check before trying to re-process; avoids hitting processing_queue for simple queries
        sa.Column("is_processed", sa.Boolean, nullable=False, server_default="false"),
    )

    # -------------------------------------------------------------------------
    # tags  (flat taxonomy; category groups them, e.g. 'mood', 'texture', 'genre')
    # -------------------------------------------------------------------------
    op.create_table(
        "tags",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("category", sa.String(50), nullable=True),
    )
    op.create_index("ix_tags_name", "tags", ["name"])
    op.create_index("ix_tags_category", "tags", ["category"])

    # -------------------------------------------------------------------------
    # sample_tags  (M:M; source tracks whether tag was auto-applied or manual)
    # -------------------------------------------------------------------------
    op.create_table(
        "sample_tags",
        sa.Column("sample_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("samples.id", ondelete="CASCADE"), nullable=False, primary_key=True),
        sa.Column("tag_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, primary_key=True),
        sa.Column("source", sa.String(20), nullable=True, server_default="'auto'"),
        # 'auto' = YAMNet/CLAP pipeline; 'manual' = user-applied
    )

    # -------------------------------------------------------------------------
    # pack_samples  (M:M; links user-curated packs to samples)
    # -------------------------------------------------------------------------
    op.create_table(
        "pack_samples",
        sa.Column("pack_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("packs.id", ondelete="CASCADE"), nullable=False, primary_key=True),
        sa.Column("sample_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("samples.id", ondelete="CASCADE"), nullable=False, primary_key=True),
    )

    # -------------------------------------------------------------------------
    # collections  (user-owned playlists/folders)
    # -------------------------------------------------------------------------
    op.create_table(
        "collections",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("is_private", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )
    op.create_index("ix_collections_user_id", "collections", ["user_id"])

    # -------------------------------------------------------------------------
    # collection_items  (M:M)
    # -------------------------------------------------------------------------
    op.create_table(
        "collection_items",
        sa.Column("collection_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("collections.id", ondelete="CASCADE"), nullable=False,
                  primary_key=True),
        sa.Column("sample_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("samples.id", ondelete="CASCADE"), nullable=False,
                  primary_key=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )

    # -------------------------------------------------------------------------
    # comments
    # -------------------------------------------------------------------------
    op.create_table(
        "comments",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sample_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("samples.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )
    op.create_index("ix_comments_sample_id", "comments", ["sample_id"])

    # -------------------------------------------------------------------------
    # ratings  (1 rating per user per sample; enforced by unique constraint)
    # -------------------------------------------------------------------------
    op.create_table(
        "ratings",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sample_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("samples.id", ondelete="CASCADE"), nullable=False),
        sa.Column("score", sa.SmallInteger, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.UniqueConstraint("user_id", "sample_id", name="uq_ratings_user_sample"),
        sa.CheckConstraint("score >= 1 AND score <= 5", name="ck_ratings_score_range"),
    )

    # -------------------------------------------------------------------------
    # download_history
    # -------------------------------------------------------------------------
    op.create_table(
        "download_history",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sample_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("samples.id", ondelete="CASCADE"), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )
    op.create_index("ix_download_history_user_id", "download_history", ["user_id"])
    op.create_index("ix_download_history_sample_id", "download_history", ["sample_id"])

    # -------------------------------------------------------------------------
    # search_queries  (analytics; no vector stored — too expensive at scale)
    # -------------------------------------------------------------------------
    op.create_table(
        "search_queries",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("query_text", sa.Text, nullable=True),
        sa.Column("query_type", PgEnum("text", "audio", name="query_type", create_type=False), nullable=False,
                  server_default=sa.text("'text'")),
        sa.Column("result_count", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )

    # -------------------------------------------------------------------------
    # processing_queue  (one row per sample; drives Librosa + CLAP + YAMNet jobs)
    # -------------------------------------------------------------------------
    op.create_table(
        "processing_queue",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("sample_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("samples.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status",
                  PgEnum("pending", "processing", "done", "failed", name="processing_status", create_type=False),
                  nullable=False, server_default=sa.text("'pending'")),
        sa.Column("retry_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String(100), nullable=True),  # detect stalled jobs
        sa.Column("error_log", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )
    op.create_index("ix_processing_queue_status", "processing_queue", ["status"])
    op.create_index("ix_processing_queue_sample_id", "processing_queue", ["sample_id"])

    # -------------------------------------------------------------------------
    # api_audit_log
    # -------------------------------------------------------------------------
    op.create_table(
        "api_audit_log",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("status_code", sa.SmallInteger, nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )
    op.create_index("ix_api_audit_log_endpoint", "api_audit_log", ["endpoint"])
    op.create_index("ix_api_audit_log_user_id", "api_audit_log", ["user_id"])


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("api_audit_log")
    op.drop_table("processing_queue")
    op.drop_table("search_queries")
    op.drop_table("download_history")
    op.drop_table("ratings")
    op.drop_table("comments")
    op.drop_table("collection_items")
    op.drop_table("collections")
    op.drop_table("pack_samples")
    op.drop_table("sample_tags")
    op.drop_table("tags")
    op.drop_table("audio_metadata")
    op.drop_table("audio_embeddings")
    op.drop_table("samples")
    op.drop_table("packs")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS processing_status")
    op.execute("DROP TYPE IF EXISTS query_type")
    op.execute("DROP EXTENSION IF EXISTS vector")
    op.execute('DROP EXTENSION IF EXISTS "uuid-ossp"')
