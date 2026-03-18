import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db, AsyncSessionLocal
from app.models.audio_embedding import AudioEmbedding
from app.models.audio_metadata import AudioMetadata
from app.models.sample import Sample
from app.models.system import ProcessingQueue, ProcessingStatus
from app.models.tag import Tag, SampleTag
from app.schemas.sample import SampleOut, SampleCreate
from app.workers import registry
from app.workers.librosa_worker import extract_features

log = logging.getLogger(__name__)

router = APIRouter()

# ── Per-process pipeline serialisation ────────────────────────────────────────
#
# CLAP (PyTorch) and the Librosa/TF workers are NOT safe for concurrent
# inference from multiple threads simultaneously.  This semaphore ensures that
# at most one MIR pipeline run is active inside a given OS process at any time.
#
# Why Semaphore(1) and not a plain Lock?
#   asyncio.Semaphore is awaitable, so callers yield control to the event loop
#   while waiting — HTTP request handling, ingestion I/O, etc. all continue
#   uninterrupted.  Only the CPU-bound ML work is serialised.
#
# Cross-process safety (e.g. uvicorn + process_queue + ingest_overnight all
# running at the same time) is handled at the database level: _run_mir_pipeline
# atomically claims its queue entry before touching the ML stack, so two
# different OS processes can never process the same sample.
_pipeline_semaphore = asyncio.Semaphore(1)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[SampleOut])
async def list_samples(
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Sample)
        .options(selectinload(Sample.audio_metadata), selectinload(Sample.tags))
        .order_by(Sample.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


@router.get("/{sample_id}", response_model=SampleOut)
async def get_sample(sample_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Sample)
        .options(selectinload(Sample.audio_metadata), selectinload(Sample.tags))
        .where(Sample.id == sample_id)
    )
    sample = result.scalar_one_or_none()
    if not sample:
        raise HTTPException(status_code=404, detail="Sample not found")
    return sample


@router.post("/", response_model=SampleOut, status_code=201)
async def create_sample(
    payload: SampleCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    sample = Sample(**payload.model_dump())
    db.add(sample)
    await db.flush()  # get sample.id before committing

    db.add(ProcessingQueue(sample_id=sample.id, status=ProcessingStatus.pending))
    await db.commit()
    await db.refresh(sample)

    # _run_mir_pipeline will atomically claim this entry before starting work.
    # If process_queue or the overnight script claims it first, the background
    # task exits silently — no double processing, no race.
    background_tasks.add_task(_run_mir_pipeline, sample.id)
    return sample


# ── Tag helper ────────────────────────────────────────────────────────────────

async def _upsert_tag(
    db: AsyncSession,
    sample_id,
    tag_name: str,
    category: str,
    seen_tag_ids: set,
) -> None:
    """
    Get-or-create a Tag by name, then insert a SampleTag only if that
    (sample_id, tag_id) pair hasn't been written yet in this pipeline run.
    This prevents PK violations when YAMNet and MusiCNN produce the same label.
    """
    result = await db.execute(select(Tag).where(Tag.name == tag_name))
    tag = result.scalar_one_or_none()
    if not tag:
        tag = Tag(name=tag_name, category=category)
        db.add(tag)
        await db.flush()  # populate tag.id

    if tag.id not in seen_tag_ids:
        seen_tag_ids.add(tag.id)
        db.add(SampleTag(sample_id=sample_id, tag_id=tag.id, source="auto"))


# ── MIR pipeline ──────────────────────────────────────────────────────────────

async def _run_mir_pipeline(sample_id: uuid.UUID, claimed: bool = False) -> None:
    """
    Download audio, run all MIR workers, write results to the DB.

    Parameters
    ----------
    sample_id:
        The sample to process.
    claimed:
        If True the caller (process_queue worker) has already atomically
        set the queue entry to 'processing' via SELECT FOR UPDATE SKIP LOCKED.
        If False (default — web API background task, overnight script) this
        function performs its own atomic claim and exits silently if another
        worker got there first.

    Concurrency guarantees
    ----------------------
    • DB-level: the atomic claim (UPDATE WHERE status='pending') ensures only
      one worker processes a given sample across all OS processes.
    • Process-level: _pipeline_semaphore(1) serialises concurrent pipeline calls
      within the same process so CLAP/Librosa are never called from multiple
      threads at once.

    Connection lifetime
    -------------------
    ML inference (Librosa, CLAP, YAMNet, MusiCNN) can take 60–120 s.
    Holding an asyncpg connection idle for that long causes the server-side
    connection to be closed (Supabase PgBouncer reaps idle connections after
    ~30 s), resulting in ConnectionResetError inside the next DB call.

    To avoid this, the pipeline uses *three separate sessions*:
      Session A — claim the queue entry + fetch file_url (fast, < 1 s)
      No session — download audio + run all ML workers (slow, 60–120 s)
      Session B — write results + update queue status (fast, < 1 s)
    A separate Session C is opened only if ML raises an exception, to mark
    the queue entry as failed without relying on the possibly-broken session.
    """
    loop = asyncio.get_running_loop()

    # ── Session A: Claim + fetch file_url ─────────────────────────────────────
    file_url: str | None = None
    queue_entry_id: uuid.UUID | None = None

    async with AsyncSessionLocal() as db:
        if not claimed:
            claim = await db.execute(
                update(ProcessingQueue)
                .where(
                    ProcessingQueue.sample_id == sample_id,
                    ProcessingQueue.status == ProcessingStatus.pending,
                )
                .values(
                    status=ProcessingStatus.processing,
                    updated_at=datetime.now(timezone.utc),
                )
                .returning(ProcessingQueue.id)
            )
            await db.commit()
            if not claim.scalar_one_or_none():
                log.debug(
                    "Sample %s already claimed by another worker — skipping.", sample_id
                )
                return

        q_result = await db.execute(
            select(ProcessingQueue).where(ProcessingQueue.sample_id == sample_id)
        )
        queue_entry = q_result.scalar_one_or_none()
        if queue_entry:
            queue_entry_id = queue_entry.id

        s_result = await db.execute(select(Sample).where(Sample.id == sample_id))
        sample = s_result.scalar_one_or_none()
        if not sample:
            # Mark failed immediately — nothing to process.
            if queue_entry:
                queue_entry.status = ProcessingStatus.failed
                queue_entry.error_log = f"Sample {sample_id} not found in samples table"
                queue_entry.updated_at = datetime.now(timezone.utc)
                await db.commit()
            return
        file_url = sample.file_url
    # Session A closed — connection returned to pool before ML starts.

    # ── No DB session: download + ML inference ────────────────────────────────
    async with _pipeline_semaphore:
        error: Exception | None = None
        features = embedding_vec = tag_results = None

        try:
            async with httpx.AsyncClient(timeout=60.0) as http:
                resp = await http.get(file_url)
                resp.raise_for_status()
                audio_bytes = resp.content

            features = await loop.run_in_executor(None, extract_features, audio_bytes)

            embedding_vec = await loop.run_in_executor(
                None, registry.clap().encode_audio, audio_bytes
            )

            yamnet_worker = registry.yamnet()
            musicnn_worker = registry.musicnn()
            tag_futures = {}
            if yamnet_worker:
                tag_futures["yamnet"] = loop.run_in_executor(
                    None, yamnet_worker.predict, audio_bytes
                )
            if musicnn_worker:
                tag_futures["musicnn"] = loop.run_in_executor(
                    None, musicnn_worker.predict, audio_bytes
                )
            if tag_futures:
                values = await asyncio.gather(*tag_futures.values())
                tag_results = dict(zip(tag_futures.keys(), values))
            else:
                tag_results = {}

        except Exception as exc:
            log.exception("MIR pipeline failed for sample %s", sample_id)
            error = exc

        # ── Session B (or C): write results / mark status ──────────────────────
        async with AsyncSessionLocal() as db:
            if error is not None:
                # ML failed — mark queue entry as failed.
                if queue_entry_id:
                    await db.execute(
                        update(ProcessingQueue)
                        .where(ProcessingQueue.id == queue_entry_id)
                        .values(
                            status=ProcessingStatus.failed,
                            error_log=str(error),
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
                await db.commit()
                return

            # ML succeeded — write features, embedding, tags, done status.
            await db.execute(
                delete(AudioMetadata).where(AudioMetadata.sample_id == sample_id)
            )
            db.add(AudioMetadata(sample_id=sample_id, is_processed=True, **features))

            await db.execute(
                delete(AudioEmbedding).where(AudioEmbedding.sample_id == sample_id)
            )
            db.add(AudioEmbedding(sample_id=sample_id, embedding=embedding_vec))

            seen_tag_ids: set = set()
            for tag_name in (tag_results or {}).get("yamnet", []):
                await _upsert_tag(db, sample_id, tag_name, "yamnet", seen_tag_ids)
            for tag_name in (tag_results or {}).get("musicnn", []):
                await _upsert_tag(db, sample_id, tag_name, "musicnn", seen_tag_ids)

            if queue_entry_id:
                await db.execute(
                    update(ProcessingQueue)
                    .where(ProcessingQueue.id == queue_entry_id)
                    .values(
                        status=ProcessingStatus.done,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
            await db.commit()
