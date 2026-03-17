import asyncio
import logging
import uuid
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy import delete, select
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

    queue_entry = ProcessingQueue(sample_id=sample.id, status=ProcessingStatus.pending)
    db.add(queue_entry)

    await db.commit()
    await db.refresh(sample)

    # Kick off MIR pipeline asynchronously
    background_tasks.add_task(_run_mir_pipeline, sample.id)
    return sample


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


async def _run_mir_pipeline(sample_id: uuid.UUID) -> None:
    """
    Background task: download audio, run all four workers in a thread pool,
    write results to the DB.  Uses its own AsyncSessionLocal because it runs
    outside the request lifecycle.

    Worker calls are offloaded to run_in_executor so the event loop is not
    blocked during CPU-bound inference (librosa / PyTorch / TensorFlow).
    The shared registry singletons are reused — model weights load once.
    """
    loop = asyncio.get_running_loop()

    async with AsyncSessionLocal() as db:
        q_result = await db.execute(
            select(ProcessingQueue).where(ProcessingQueue.sample_id == sample_id)
        )
        queue_entry = q_result.scalar_one_or_none()
        if queue_entry:
            queue_entry.status = ProcessingStatus.processing
            await db.commit()

        try:
            s_result = await db.execute(select(Sample).where(Sample.id == sample_id))
            sample = s_result.scalar_one_or_none()
            if not sample:
                raise ValueError(f"Sample {sample_id} not found")

            # Download audio bytes from Supabase Storage
            async with httpx.AsyncClient(timeout=60.0) as http:
                resp = await http.get(sample.file_url)
                resp.raise_for_status()
                audio_bytes = resp.content

            # ── 1. Librosa — audio features (CPU-bound, run in thread) ────────
            features = await loop.run_in_executor(
                None, extract_features, audio_bytes
            )
            # Upsert: delete any previously-written rows on retry so INSERT succeeds.
            existing_meta = await db.execute(
                select(AudioMetadata).where(AudioMetadata.sample_id == sample_id)
            )
            if existing_meta.scalar_one_or_none():
                await db.execute(
                    delete(AudioMetadata).where(AudioMetadata.sample_id == sample_id)
                )
            db.add(AudioMetadata(sample_id=sample_id, is_processed=True, **features))

            # ── 2. CLAP — 512-dim embedding (CPU/GPU-bound, run in thread) ────
            embedding_vec = await loop.run_in_executor(
                None, registry.clap().encode_audio, audio_bytes
            )
            existing_emb = await db.execute(
                select(AudioEmbedding).where(AudioEmbedding.sample_id == sample_id)
            )
            if existing_emb.scalar_one_or_none():
                await db.execute(
                    delete(AudioEmbedding).where(AudioEmbedding.sample_id == sample_id)
                )
            db.add(AudioEmbedding(sample_id=sample_id, embedding=embedding_vec))

            # ── 3 & 4. YAMNet + MusiCNN — run concurrently in thread pool ─────
            # Workers may return None if TF/MusiCNN is unavailable; skip gracefully.
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

            tag_results = {}
            if tag_futures:
                results = await asyncio.gather(*tag_futures.values())
                tag_results = dict(zip(tag_futures.keys(), results))

            # Merge results; skip duplicate (sample_id, tag_id) pairs
            seen_tag_ids: set = set()
            for tag_name in tag_results.get("yamnet", []):
                await _upsert_tag(db, sample_id, tag_name, "yamnet", seen_tag_ids)
            for tag_name in tag_results.get("musicnn", []):
                await _upsert_tag(db, sample_id, tag_name, "musicnn", seen_tag_ids)

            if queue_entry:
                queue_entry.status = ProcessingStatus.done
            await db.commit()

        except Exception as exc:
            log.exception("MIR pipeline failed for sample %s", sample_id)
            if queue_entry:
                queue_entry.status = ProcessingStatus.failed
                queue_entry.error_log = str(exc)
            await db.commit()
