#!/usr/bin/env python3
"""
Ingest Freesound samples matching a query into the database.

Downloads HQ MP3 previews, uploads them to Supabase Storage, inserts Sample
rows, and queues each sample for MIR processing.

Usage (from repo root):
    # Ingest only (MIR pipeline runs later via API or process_queue):
    python -m scripts.ingest_freesound "kick drum" --limit 500

    # Ingest and immediately run the full MIR pipeline on each sample:
    python -m scripts.ingest_freesound "ambient pad" --process
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow running directly from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from supabase import create_client

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.sample import Sample
from app.models.system import ProcessingQueue, ProcessingStatus
from app.routers.samples import _run_mir_pipeline
from app.scraper.freesound import FreesoundClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


async def ingest(query: str, limit: int, process: bool) -> None:
    supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    bucket = settings.SUPABASE_STORAGE_BUCKET
    ingested = 0

    async with FreesoundClient() as client, AsyncSessionLocal() as db:
        async for sound in client.iter_all_sounds(query):
            if limit and ingested >= limit:
                break

            freesound_id = sound["id"]

            # Skip sounds already present in the DB.
            existing = await db.execute(
                select(Sample).where(Sample.freesound_id == freesound_id)
            )
            if existing.scalar_one_or_none():
                log.info("Skipping %s (already ingested)", freesound_id)
                continue

            # Prefer the HQ MP3 preview; fall back to LQ.
            previews = sound.get("previews", {})
            preview_url = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
            if not preview_url:
                log.warning("Sound %s has no preview URL — skipping", freesound_id)
                continue

            try:
                audio_bytes = await client.download_preview(preview_url)
            except Exception as exc:
                log.warning("Download failed for %s: %s", freesound_id, exc)
                continue

            # Upload to Supabase Storage under freesound/<id>.mp3
            storage_path = f"freesound/{freesound_id}.mp3"
            try:
                supabase.storage.from_(bucket).upload(
                    storage_path,
                    audio_bytes,
                    {"content-type": "audio/mpeg", "upsert": "true"},
                )
                public_url = supabase.storage.from_(bucket).get_public_url(storage_path)
            except Exception as exc:
                log.warning("Supabase upload failed for %s: %s", freesound_id, exc)
                continue

            duration_s = sound.get("duration")
            duration_ms = int(duration_s * 1000) if duration_s is not None else None

            sample = Sample(
                title=sound.get("name", f"freesound-{freesound_id}"),
                freesound_id=freesound_id,
                file_url=public_url,
                duration_ms=duration_ms,
                file_size_bytes=sound.get("filesize"),
                mime_type="audio/mpeg",
            )
            db.add(sample)

            try:
                await db.flush()  # populate sample.id before adding queue entry
                db.add(ProcessingQueue(sample_id=sample.id, status=ProcessingStatus.pending))
                await db.commit()
                await db.refresh(sample)
                ingested += 1
                log.info("[%d] Ingested %s — %s", ingested, freesound_id, sample.title)
            except Exception as exc:
                await db.rollback()
                log.warning("DB insert failed for %s: %s", freesound_id, exc)
                continue

            # Optionally run the full MIR pipeline inline.
            # --process is intentionally off by default because loading CLAP (~900 MB),
            # YAMNet, and MusiCNN for every sample makes bulk ingestion very slow.
            # Use a separate process_queue worker for large batches.
            if process:
                log.info("Running MIR pipeline for %s …", sample.id)
                await _run_mir_pipeline(sample.id)

    log.info("Done. Ingested %d sample(s) for query %r.", ingested, query)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest Freesound audio samples into the database."
    )
    parser.add_argument("query", help="Freesound text search query, e.g. 'kick drum'")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Stop after ingesting N sounds (default: 0 = no limit)",
    )
    parser.add_argument(
        "--process",
        action="store_true",
        help=(
            "Run the full MIR pipeline (Librosa, CLAP, YAMNet, MusiCNN) on each "
            "sample immediately after ingestion. Slow for large batches; omit this "
            "flag and run scripts/process_queue.py separately instead."
        ),
    )
    args = parser.parse_args()
    asyncio.run(ingest(args.query, args.limit, args.process))


if __name__ == "__main__":
    main()
