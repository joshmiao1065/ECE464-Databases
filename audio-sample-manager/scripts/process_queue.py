#!/usr/bin/env python3
"""
Batch MIR pipeline worker — processes samples stuck in processing_queue.

Polls the database for rows with status='pending' (or stale 'processing' rows
that have been stuck longer than --stale-minutes) and runs _run_mir_pipeline
on each one.  Runs until all pending work is exhausted or until interrupted
with Ctrl-C / SIGTERM.

Usage (from repo root):
    python -m scripts.process_queue
    python -m scripts.process_queue --poll-interval 5 --max-retries 2 --stale-minutes 10
    python -m scripts.process_queue --once          # process current backlog then exit
"""

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.database import AsyncSessionLocal
from app.models.system import ProcessingQueue, ProcessingStatus
from app.routers.samples import _run_mir_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("process_queue")

# Unique identifier for this worker process (used in worker_id column for stall detection).
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"

_shutdown = False


def _install_signal_handlers() -> None:
    """Set SIGTERM / SIGINT to trigger a graceful shutdown after the current job."""
    def _handle(signum, frame):  # noqa: ARG001
        global _shutdown
        log.info("Signal %s received — will stop after current job.", signum)
        _shutdown = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


async def _claim_pending(db, stale_cutoff: datetime) -> ProcessingQueue | None:
    """
    Atomically claim one pending queue entry for this worker.

    Also resets stale 'processing' entries (stuck longer than stale_cutoff)
    back to 'pending' so they can be retried.

    Returns the claimed entry or None if the queue is empty.
    """
    # Reset stale entries first so they are visible to the claim query below.
    await db.execute(
        update(ProcessingQueue)
        .where(
            ProcessingQueue.status == ProcessingStatus.processing,
            ProcessingQueue.updated_at < stale_cutoff,
        )
        .values(
            status=ProcessingStatus.pending,
            worker_id=None,
            updated_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    # Fetch the oldest pending entry.
    result = await db.execute(
        select(ProcessingQueue)
        .where(ProcessingQueue.status == ProcessingStatus.pending)
        .order_by(ProcessingQueue.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    entry = result.scalar_one_or_none()
    if not entry:
        return None

    # Claim it.
    entry.status = ProcessingStatus.processing
    entry.worker_id = WORKER_ID
    entry.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return entry


async def run_worker(poll_interval: int, max_retries: int, stale_minutes: int, once: bool) -> None:
    stale_cutoff_delta = timedelta(minutes=stale_minutes)
    processed = 0
    skipped = 0

    log.info("Worker %s starting. poll_interval=%ds max_retries=%d stale_minutes=%d",
             WORKER_ID, poll_interval, max_retries, stale_minutes)

    while not _shutdown:
        stale_cutoff = datetime.now(timezone.utc) - stale_cutoff_delta

        async with AsyncSessionLocal() as db:
            entry = await _claim_pending(db, stale_cutoff)

        if entry is None:
            if once:
                log.info("Queue empty. Processed %d sample(s), skipped %d.", processed, skipped)
                return
            log.debug("Queue empty — sleeping %ds.", poll_interval)
            await asyncio.sleep(poll_interval)
            continue

        sample_id: uuid.UUID = entry.sample_id
        retry = entry.retry_count

        if retry >= max_retries:
            log.warning("Sample %s has %d retries (max %d) — marking failed.", sample_id, retry, max_retries)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ProcessingQueue).where(ProcessingQueue.id == entry.id)
                )
                q = result.scalar_one_or_none()
                if q:
                    q.status = ProcessingStatus.failed
                    q.error_log = f"Exceeded max_retries ({max_retries})"
                    q.updated_at = datetime.now(timezone.utc)
                    await db.commit()
            skipped += 1
            continue

        log.info("[job] Processing sample %s (retry=%d) …", sample_id, retry)
        try:
            await _run_mir_pipeline(sample_id)
            processed += 1
            log.info("[job] Done — sample %s. Total processed: %d.", sample_id, processed)
        except Exception:
            log.exception("[job] Pipeline raised an exception for sample %s.", sample_id)
            # Increment retry_count so we eventually give up.
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ProcessingQueue).where(ProcessingQueue.id == entry.id)
                )
                q = result.scalar_one_or_none()
                if q:
                    q.retry_count = retry + 1
                    q.updated_at = datetime.now(timezone.utc)
                    # _run_mir_pipeline already sets status=failed on its own exception path,
                    # but reset to pending here so the outer retry loop can reclaim it.
                    q.status = ProcessingStatus.pending
                    await db.commit()

        if _shutdown:
            break

    log.info("Shutdown. Processed %d sample(s), skipped %d.", processed, skipped)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process pending MIR pipeline jobs from processing_queue."
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=10,
        metavar="SECONDS",
        help="Seconds to sleep between queue polls when the queue is empty (default: 10)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        metavar="N",
        help="Skip samples that have already failed N times (default: 3)",
    )
    parser.add_argument(
        "--stale-minutes",
        type=int,
        default=15,
        metavar="MINUTES",
        help=(
            "Minutes after which a 'processing' entry with no update is considered stale "
            "and reset to 'pending' (default: 15)"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process all current pending jobs then exit instead of polling continuously.",
    )
    args = parser.parse_args()

    _install_signal_handlers()
    asyncio.run(run_worker(args.poll_interval, args.max_retries, args.stale_minutes, args.once))


if __name__ == "__main__":
    main()
