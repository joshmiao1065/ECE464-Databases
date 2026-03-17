#!/usr/bin/env python3
"""
Retag already-processed samples with YAMNet and MusiCNN auto-tags.

Targets samples whose processing_queue status is 'done' but that have no
auto-generated tags (i.e. they were processed before YAMNet/MusiCNN were
available). Skips Librosa and CLAP — only the tagging step runs.

Usage (from repo root):
    python -m scripts.retag_samples
    python -m scripts.retag_samples --limit 100   # stop after 100 samples
    python -m scripts.retag_samples --dry-run     # show count only
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from sqlalchemy import select, text, delete

from app.database import AsyncSessionLocal
from app.models.sample import Sample
from app.models.tag import Tag, SampleTag
from app.workers import registry  # sets TF_USE_LEGACY_KERAS=1 before any TF import

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("retag_samples")

_shutdown = False


def _install_signal_handlers():
    def _handle(signum, frame):
        global _shutdown
        log.info("Signal %s received — will stop after current sample.", signum)
        _shutdown = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


async def _upsert_tag(db, sample_id, tag_name: str, category: str, seen_tag_ids: set) -> None:
    result = await db.execute(select(Tag).where(Tag.name == tag_name))
    tag = result.scalar_one_or_none()
    if not tag:
        tag = Tag(name=tag_name, category=category)
        db.add(tag)
        await db.flush()

    if tag.id not in seen_tag_ids:
        seen_tag_ids.add(tag.id)
        db.add(SampleTag(sample_id=sample_id, tag_id=tag.id, source="auto"))


async def fetch_untagged_samples(limit: int | None) -> list:
    """Return Sample rows that are 'done' but have no auto-tags."""
    async with AsyncSessionLocal() as db:
        query = text("""
            SELECT s.id, s.file_url
            FROM samples s
            JOIN processing_queue pq ON pq.sample_id = s.id
            WHERE pq.status = 'done'
              AND NOT EXISTS (
                  SELECT 1 FROM sample_tags st
                  WHERE st.sample_id = s.id AND st.source = 'auto'
              )
            ORDER BY s.created_at ASC
        """ + (f" LIMIT {int(limit)}" if limit else ""))
        result = await db.execute(query)
        return result.fetchall()


async def retag_sample(sample_id, file_url: str, yamnet_worker, musicnn_worker) -> bool:
    """Download audio and write YAMNet + MusiCNN tags. Returns True on success."""
    loop = asyncio.get_running_loop()

    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.get(file_url)
            resp.raise_for_status()
            audio_bytes = resp.content

        tag_futures = {}
        if yamnet_worker:
            tag_futures["yamnet"] = loop.run_in_executor(
                None, yamnet_worker.predict, audio_bytes
            )
        if musicnn_worker:
            tag_futures["musicnn"] = loop.run_in_executor(
                None, musicnn_worker.predict, audio_bytes
            )

        if not tag_futures:
            log.warning("Neither YAMNet nor MusiCNN available — nothing to do.")
            return False

        values = await asyncio.gather(*tag_futures.values())
        tag_results = dict(zip(tag_futures.keys(), values))

        async with AsyncSessionLocal() as db:
            # Remove any stale auto-tags from a previous partial attempt.
            await db.execute(
                delete(SampleTag).where(
                    SampleTag.sample_id == sample_id,
                    SampleTag.source == "auto",
                )
            )
            seen_tag_ids: set = set()
            for tag_name in tag_results.get("yamnet", []):
                await _upsert_tag(db, sample_id, tag_name, "yamnet", seen_tag_ids)
            for tag_name in tag_results.get("musicnn", []):
                await _upsert_tag(db, sample_id, tag_name, "musicnn", seen_tag_ids)
            await db.commit()

        return True

    except Exception:
        log.exception("Retag failed for sample %s", sample_id)
        return False


async def run(limit: int | None, dry_run: bool) -> None:
    rows = await fetch_untagged_samples(limit)
    total = len(rows)

    if dry_run or total == 0:
        print(f"Samples to retag: {total}")
        return

    log.info("Retagging %d sample(s) with YAMNet + MusiCNN.", total)

    yamnet_worker = registry.yamnet()
    musicnn_worker = registry.musicnn()

    if not yamnet_worker and not musicnn_worker:
        log.error("Both YAMNet and MusiCNN are unavailable — nothing to do.")
        sys.exit(1)

    log.info(
        "Workers available: YAMNet=%s  MusiCNN=%s",
        "YES" if yamnet_worker else "NO",
        "YES" if musicnn_worker else "NO",
    )

    done = 0
    failed = 0

    for i, row in enumerate(rows, 1):
        if _shutdown:
            break

        log.info("[%d/%d] Retagging sample %s …", i, total, row.id)
        ok = await retag_sample(row.id, row.file_url, yamnet_worker, musicnn_worker)
        if ok:
            done += 1
        else:
            failed += 1

    log.info("Done. %d retagged, %d failed, %d skipped by shutdown.",
             done, failed, total - done - failed)


def main():
    parser = argparse.ArgumentParser(
        description="Add YAMNet + MusiCNN auto-tags to already-processed samples."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of samples to retag (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print count of samples that would be retagged and exit.",
    )
    args = parser.parse_args()

    _install_signal_handlers()
    asyncio.run(run(args.limit, args.dry_run))


if __name__ == "__main__":
    main()
