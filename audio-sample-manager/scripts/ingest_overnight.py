#!/usr/bin/env python3
"""
Overnight Freesound ingestion + MIR processing.

Runs two concurrent async tasks in a single process:

  Producer  — pages through Freesound search results, uploads audio to Supabase
              Storage, inserts Sample + ProcessingQueue rows, and puts each new
              sample ID onto an internal asyncio.Queue.

  Consumer  — reads sample IDs from the queue and runs the full MIR pipeline
              (Librosa features + CLAP embeddings) on each one.  Only one
              pipeline runs at a time thanks to the shared _pipeline_semaphore
              in app.routers.samples — this keeps RAM usage flat and avoids
              concurrent PyTorch inference bugs.

Key safety properties
─────────────────────
• No duplicate ingestion: checks freesound_id against the DB before downloading.
• No duplicate processing: _run_mir_pipeline does an atomic DB claim
  (UPDATE WHERE status='pending') before touching any ML model.  If the web
  server's background task or a separately running process_queue worker claims
  the entry first, the consumer silently skips it.
• Crash-safe ingestion state: a JSON state file records completed queries, the
  current in-progress query/page, and today's API request count.  Ctrl-C or a
  crash can be resumed exactly where it left off.
• Graceful shutdown on rate limit: when the Freesound 429 is hit or the daily
  request budget is exhausted, the producer signals the consumer to drain all
  remaining queued IDs through the pipeline before exiting.

Usage (from repo root, with venv active):
    python -m scripts.ingest_overnight              # default: process inline
    python -m scripts.ingest_overnight --no-process # ingest only (faster)
    python -m scripts.ingest_overnight --max-requests 500
    python -m scripts.ingest_overnight --reset      # discard state, start fresh

State file: scripts/ingest_state.json  (delete to reset)
Log file:   scripts/ingest_overnight.log
"""

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
import uuid
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from sqlalchemy import select
from supabase import create_client

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.sample import Sample
from app.models.system import ProcessingQueue, ProcessingStatus
from app.routers.samples import _run_mir_pipeline

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE = Path(__file__).parent / "ingest_overnight.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="a"),
    ],
)
log = logging.getLogger(__name__)

# ── State file ────────────────────────────────────────────────────────────────

STATE_FILE = Path(__file__).parent / "ingest_state.json"

# ── Sentinel value used to signal the consumer to stop ────────────────────────

_DONE = None

# ── Freesound search queries ───────────────────────────────────────────────────
# ~300 queries spanning instruments, genres, textures, foley, nature, synthesis.
# Add freely — the shuffled order changes each new day so every run covers
# different ground even if interrupted.

QUERIES = [
    # Drum / percussion elements
    "kick drum", "snare drum", "hi hat", "open hi hat", "closed hi hat",
    "cymbal crash", "cymbal ride", "drum fill", "drum roll", "clap",
    "rim shot", "cowbell", "tambourine", "shaker", "bongo",
    "conga drum", "djembe", "cajon", "tabla", "timbales",
    "woodblock", "triangle", "snare brush", "ghost note snare",
    "808 drum", "drum machine", "trap hi hat", "drill snare", "breakbeat",
    "drum loop", "percussion loop", "808 bass hit", "sub bass hit",
    "tom drum", "floor tom", "bass drum",
    # Bass
    "bass guitar", "electric bass", "upright bass", "slap bass",
    "fretless bass", "fingerpicked bass", "bass loop", "sub bass",
    "808 bass", "synth bass", "bass riff", "bass run",
    "deep bass", "dirty bass", "reese bass", "wobble bass",
    # Guitar
    "acoustic guitar", "electric guitar", "guitar strum", "guitar riff",
    "guitar chord", "guitar loop", "fingerpicked guitar", "guitar slide",
    "guitar bend", "guitar harmonic", "distorted guitar", "clean guitar",
    "guitar arpeggio", "rhythm guitar", "lead guitar", "guitar solo",
    "nylon string guitar", "steel string guitar", "guitar noise",
    "guitar feedback", "lap steel guitar", "dobro",
    # Piano / keys
    "piano", "grand piano", "upright piano", "piano chord", "piano melody",
    "piano loop", "piano note", "electric piano", "rhodes piano",
    "wurlitzer", "harpsichord", "clavinet", "prepared piano",
    "piano arpeggio", "piano glissando", "toy piano",
    # Synthesizers
    "synth pad", "synth arp", "synth lead", "synth pluck", "synth bass",
    "analog synth", "modular synth", "moog synth", "synth chord",
    "synth melody", "synth loop", "wavetable synth", "fm synthesis",
    "synth texture", "synth drone", "synth noise", "synth sweep",
    "oscillator", "saw wave", "square wave", "sine wave", "noise synth",
    "filter sweep", "envelope synth",
    # Strings
    "violin", "viola", "cello", "double bass", "string quartet",
    "orchestral strings", "string ensemble", "pizzicato", "arco strings",
    "violin melody", "cello melody", "string pad", "string glissando",
    "string tremolo", "col legno", "string staccato",
    "harp", "harp glissando", "harp arpeggio",
    # Brass
    "trumpet", "trombone", "french horn", "tuba", "brass section",
    "brass stab", "muted trumpet", "flugelhorn",
    "brass fanfare", "brass loop", "brass ensemble",
    # Woodwinds
    "flute", "clarinet", "oboe", "bassoon", "saxophone",
    "alto saxophone", "tenor saxophone", "soprano saxophone",
    "flute melody", "flute breathy", "pan flute", "recorder",
    "piccolo", "english horn", "shakuhachi", "bansuri",
    # Vocals
    "vocal chop", "vocal sample", "vocal loop", "vocal ad lib",
    "choir", "female voice", "male voice", "vocal harmony",
    "vocal texture", "vocal breath", "vocal stutter", "a cappella",
    "gospel choir", "gregorian chant", "vocal melody", "scat singing",
    "vocal drone", "spoken word", "whisper",
    # World / ethnic
    "sitar", "sarod", "koto", "shamisen", "erhu",
    "oud", "bouzouki", "balalaika", "mandolin", "banjo",
    "ukulele", "steel drum", "marimba", "xylophone", "vibraphone",
    "gamelan", "mbira", "kalimba", "didgeridoo",
    "duduk", "darbuka", "doumbek",
    # Electronic / EDM
    "house music loop", "techno loop", "drum and bass loop",
    "trap beat", "hip hop drum loop", "lo fi beat",
    "ambient electronic", "glitch sound", "stuttered beat",
    "dubstep wobble", "jungle break", "amen break",
    "trance loop", "progressive house", "deep house",
    # Ambient / texture
    "ambient pad", "ambient drone", "atmospheric texture",
    "dark ambient", "space ambient", "cinematic ambient",
    "soundscape", "evolving texture", "granular texture",
    "industrial ambience", "night ambience", "city ambience", "wind ambience",
    # Nature / field recordings
    "rain", "thunder", "storm",
    "ocean waves", "river water", "waterfall", "stream",
    "birds chirping", "birdsong", "owl", "frog",
    "cricket", "cicada", "wind", "rustling leaves",
    "fire crackling", "snow footsteps",
    # Urban / foley
    "footsteps", "door knock", "door creak", "door slam",
    "glass breaking", "paper crumple",
    "keyboard typing", "phone ring",
    "car engine", "car horn", "traffic",
    "train", "helicopter", "airplane",
    # Designed / SFX
    "vinyl crackle", "vinyl noise", "tape hiss",
    "white noise", "pink noise",
    "explosion", "impact hit", "whoosh",
    "laser", "sci fi", "glitch", "bitcrushed",
    "riser", "downlifter", "transition sfx", "sweep fx",
    "reverse cymbal", "reverse snare", "reverse piano",
    "pitched down", "time stretched",
    # Moods
    "dark", "eerie", "haunting", "melancholic",
    "uplifting", "energetic", "aggressive", "tension",
    "suspense", "cinematic", "epic", "dramatic", "peaceful",
    "meditative", "nostalgic",
    # Loops and phrases
    "guitar loop", "piano loop", "bass loop", "synth loop",
    "vocal loop", "string loop", "horn loop", "flute loop",
    "chord progression", "melody loop",
    "jazz loop", "blues loop", "funk loop", "soul loop",
    "latin loop", "bossa nova", "samba", "reggae loop",
    "afrobeat", "highlife", "cumbia",
]

# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _build_fresh_state() -> dict:
    shuffled = list(QUERIES)
    random.shuffle(shuffled)
    return {
        "date": str(date.today()),
        "requests_used": 0,
        "total_ingested": 0,
        "queue": shuffled,
        "completed": [],
        "in_progress": None,   # {"query": str, "page": int}
    }


def _get_state(reset: bool) -> dict:
    if reset:
        log.info("--reset: starting fresh.")
        return _build_fresh_state()

    state = _load_state()
    if not state:
        log.info("No state file found — starting fresh.")
        return _build_fresh_state()

    today = str(date.today())
    if state.get("date") != today:
        log.info("New day (%s) — request counter reset.", today)
        state["date"] = today
        state["requests_used"] = 0
        # Refill the queue if exhausted
        if not state.get("queue") and not state.get("in_progress"):
            remaining = [q for q in QUERIES if q not in state.get("completed", [])]
            if not remaining:
                remaining = list(QUERIES)
                state["completed"] = []
            random.shuffle(remaining)
            state["queue"] = remaining
            log.info("Query queue refilled with %d queries.", len(remaining))
        _save_state(state)
    else:
        log.info(
            "Resuming: %d requests used today, %d queries in queue.",
            state.get("requests_used", 0),
            len(state.get("queue", [])) + (1 if state.get("in_progress") else 0),
        )

    return state


# ── Producer: ingest from Freesound ──────────────────────────────────────────

PAGE_SIZE = 150          # max Freesound allows
RATE_LIMIT_BACKOFF = 65  # seconds to wait after a 429


async def _producer(
    state: dict,
    max_requests: int,
    max_per_query: int,
    pipeline_queue: asyncio.Queue,
    process_inline: bool,
) -> None:
    """
    Page through QUERIES, ingest every new sound, and put sample IDs onto
    pipeline_queue for the consumer.  Stops on rate limit or budget exhaustion
    and puts a _DONE sentinel so the consumer knows to drain and exit.

    max_per_query caps how many new tracks are ingested per query term before
    moving on.  Already-seen sounds (duplicate check) don't count toward the
    cap — only newly inserted rows do.  Set to 0 for no limit.
    """
    from app.scraper.freesound import FreesoundClient

    supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    bucket = settings.SUPABASE_STORAGE_BUCKET

    # Mutable counter shared between _ingest_page and the outer loop.
    # Reset to ip["ingested"] on resume so the cap is respected across restarts.
    query_ingested = [0]

    async def _ingest_page(client, query: str, page: int) -> str:
        """Fetch one search page, ingest each new sound.  Returns a status string."""
        if state["requests_used"] >= max_requests:
            return "budget"

        log.info(
            "[req %d/%d] query=%r page=%d ingested_this_query=%d",
            state["requests_used"] + 1, max_requests, query, page, query_ingested[0],
        )

        try:
            data = await client.search_sounds(query, page=page, page_size=PAGE_SIZE)
            state["requests_used"] += 1
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                log.warning("429 rate limited — backing off %ds.", RATE_LIMIT_BACKOFF)
                _save_state(state)
                await asyncio.sleep(RATE_LIMIT_BACKOFF)
                return "rate_limited"
            log.error("HTTP error searching Freesound: %s", exc)
            await asyncio.sleep(5)
            return "error"
        except Exception as exc:
            log.error("Search error: %s", exc)
            await asyncio.sleep(5)
            return "error"

        results = data.get("results", [])
        if not results:
            return "done"

        async with AsyncSessionLocal() as db:
            for sound in results:
                # Per-query cap: stop ingesting from this query and move on.
                if max_per_query and query_ingested[0] >= max_per_query:
                    return "quota"

                freesound_id = sound.get("id")
                if not freesound_id:
                    continue

                # Duplicate check — already-seen sounds don't count toward cap.
                if (await db.execute(
                    select(Sample).where(Sample.freesound_id == freesound_id)
                )).scalar_one_or_none():
                    continue

                previews = sound.get("previews", {})
                preview_url = (
                    previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3")
                )
                if not preview_url:
                    continue

                try:
                    audio_bytes = await client.download_preview(preview_url)
                except Exception as exc:
                    log.warning("Download failed %s: %s", freesound_id, exc)
                    continue

                storage_path = f"freesound/{freesound_id}.mp3"
                try:
                    supabase.storage.from_(bucket).upload(
                        storage_path, audio_bytes,
                        {"content-type": "audio/mpeg", "upsert": "true"},
                    )
                    public_url = supabase.storage.from_(bucket).get_public_url(
                        storage_path
                    )
                except Exception as exc:
                    log.warning("Storage upload failed %s: %s", freesound_id, exc)
                    continue

                dur = sound.get("duration")
                sample = Sample(
                    title=sound.get("name", f"freesound-{freesound_id}"),
                    freesound_id=freesound_id,
                    file_url=public_url,
                    duration_ms=int(dur * 1000) if dur is not None else None,
                    file_size_bytes=sound.get("filesize"),
                    mime_type="audio/mpeg",
                )
                db.add(sample)
                try:
                    await db.flush()
                    db.add(
                        ProcessingQueue(
                            sample_id=sample.id, status=ProcessingStatus.pending
                        )
                    )
                    await db.commit()
                    await db.refresh(sample)
                    query_ingested[0] += 1
                    state["total_ingested"] += 1
                    log.info(
                        "[+%d] Ingested freesound:%s — %s",
                        state["total_ingested"], freesound_id, sample.title[:60],
                    )
                    if process_inline:
                        # Hand the sample ID to the consumer for MIR processing.
                        await pipeline_queue.put(sample.id)
                except Exception as exc:
                    await db.rollback()
                    log.warning("DB insert failed %s: %s", freesound_id, exc)
                    continue

        return "done" if not data.get("next") else "continue"

    async with FreesoundClient() as client:
        # Resume any partially-completed query first.
        # Restore the per-query counter so the cap is respected on resume.
        ip = state.get("in_progress")
        if ip:
            q, pg = ip["query"], ip.get("page", 1)
            query_ingested[0] = ip.get("ingested", 0)
            log.info("Resuming mid-query %r from page %d (%d already ingested)", q, pg, query_ingested[0])
            while True:
                status = await _ingest_page(client, q, pg)
                if status in ("done", "quota", "rate_limited", "budget"):
                    if status in ("done", "quota"):
                        state["completed"].append(q)
                        state["in_progress"] = None
                    _save_state(state)
                    if status in ("rate_limited", "budget"):
                        await pipeline_queue.put(_DONE)
                        return
                    break
                pg += 1
                state["in_progress"] = {"query": q, "page": pg, "ingested": query_ingested[0]}
                _save_state(state)

        # Work through the remaining queue
        while state.get("queue"):
            if state["requests_used"] >= max_requests:
                log.warning("Request budget reached (%d/%d).", state["requests_used"], max_requests)
                break

            query = state["queue"].pop(0)
            query_ingested[0] = 0
            state["in_progress"] = {"query": query, "page": 1, "ingested": 0}
            _save_state(state)
            cap_str = f"cap={max_per_query}" if max_per_query else "no cap"
            log.info("── Query: %r  (%d remaining, %s) ──", query, len(state["queue"]), cap_str)

            page = 1
            while True:
                status = await _ingest_page(client, query, page)
                if status in ("done", "quota", "rate_limited", "budget"):
                    if status in ("done", "quota"):
                        if status == "quota":
                            log.info("Per-query cap (%d) reached for %r — moving on.", max_per_query, query)
                        state["completed"].append(query)
                        state["in_progress"] = None
                    _save_state(state)
                    if status in ("rate_limited", "budget"):
                        await pipeline_queue.put(_DONE)
                        return
                    break
                page += 1
                state["in_progress"] = {"query": query, "page": page, "ingested": query_ingested[0]}
                _save_state(state)
        else:
            log.info("All queries exhausted — run again tomorrow for a fresh shuffle.")

    log.info(
        "Ingestion complete. Requests used: %d/%d. Total ingested this run: %d.",
        state["requests_used"], max_requests, state["total_ingested"],
    )
    await pipeline_queue.put(_DONE)


# ── Consumer: run MIR pipeline ────────────────────────────────────────────────

async def _consumer(pipeline_queue: asyncio.Queue) -> None:
    """
    Pull sample IDs from pipeline_queue and run _run_mir_pipeline on each.

    Safety:
    • Uses claimed=False so _run_mir_pipeline does an atomic DB claim before
      starting.  If the web API's background task or a separately running
      process_queue worker already claimed the entry, the call is a no-op.
    • _pipeline_semaphore (inside _run_mir_pipeline) ensures only one CLAP /
      Librosa inference runs at a time within this process — no concurrent
      PyTorch calls, no memory spikes.

    The consumer keeps running until it receives the _DONE sentinel, then drains
    any remaining IDs in the queue before exiting so no ingested sample is left
    unprocessed.
    """
    done_seen = False
    queued_after_done: list[uuid.UUID] = []

    while True:
        sample_id = await pipeline_queue.get()
        pipeline_queue.task_done()

        if sample_id is _DONE:
            # Producer finished — drain whatever is still in the queue, then exit.
            done_seen = True
            # Drain synchronously from here
            while not pipeline_queue.empty():
                remaining = await pipeline_queue.get()
                pipeline_queue.task_done()
                if remaining is not _DONE:
                    queued_after_done.append(remaining)
            break

        log.info("[pipeline] Processing sample %s …", sample_id)
        try:
            # claimed=False → _run_mir_pipeline atomically claims before ML work.
            await _run_mir_pipeline(sample_id, claimed=False)
        except Exception:
            log.exception("[pipeline] Unexpected error for sample %s", sample_id)

    # Process any IDs that arrived between the DONE sentinel and queue drain
    for sample_id in queued_after_done:
        log.info("[pipeline] Draining: %s …", sample_id)
        try:
            await _run_mir_pipeline(sample_id, claimed=False)
        except Exception:
            log.exception("[pipeline] Drain error for sample %s", sample_id)

    log.info("[pipeline] Consumer finished.")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(max_requests: int, max_per_query: int, reset: bool, process_inline: bool) -> None:
    state = _get_state(reset)
    _save_state(state)

    log.info("=" * 70)
    log.info("Overnight ingestion starting")
    log.info("  Inline MIR processing : %s", "YES" if process_inline else "NO (use process_queue separately)")
    log.info("  Request budget        : %d/day", max_requests)
    log.info("  Per-query cap         : %s", max_per_query if max_per_query else "unlimited")
    log.info("  Queries in queue      : %d", len(state.get("queue", [])))
    log.info("  Requests used today   : %d", state.get("requests_used", 0))
    log.info("  Total ingested (ever) : %d", state.get("total_ingested", 0))
    log.info("=" * 70)

    if process_inline:
        # Bounded queue — if the consumer falls behind by more than 100 samples
        # the producer will await pipeline_queue.put(), giving the consumer time
        # to catch up.  This prevents unbounded memory growth when CLAP is slow.
        pipeline_queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        # Install graceful Ctrl-C handler: let current pipeline job finish.
        loop = asyncio.get_running_loop()
        _interrupted = asyncio.Event()

        def _sigint_handler():
            if not _interrupted.is_set():
                log.info("Interrupt received — finishing current pipeline job then exiting.")
                _interrupted.set()
                # Put sentinel so the consumer knows to drain and exit
                pipeline_queue.put_nowait(_DONE)

        loop.add_signal_handler(signal.SIGINT, _sigint_handler)
        loop.add_signal_handler(signal.SIGTERM, _sigint_handler)

        # Run producer and consumer concurrently.
        # If the producer is interrupted, it puts a _DONE sentinel and returns;
        # the consumer drains remaining queue entries before stopping.
        await asyncio.gather(
            _producer(state, max_requests, max_per_query, pipeline_queue, process_inline=True),
            _consumer(pipeline_queue),
        )
    else:
        # Ingestion only — no consumer needed; queue entries stay 'pending' for
        # a separate `python -m scripts.process_queue` run.
        dummy_queue: asyncio.Queue = asyncio.Queue()
        await _producer(state, max_requests, max_per_query, dummy_queue, process_inline=False)
        # Consume the single _DONE sentinel the producer puts at the end.
        await dummy_queue.get()

    log.info("=" * 70)
    log.info(
        "Session done. Requests used: %d/%d. Samples ingested this run: %d.",
        state["requests_used"], max_requests, state["total_ingested"],
    )
    log.info("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Overnight Freesound ingestion with optional inline MIR processing."
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=1900,
        metavar="N",
        help=(
            "Maximum Freesound API requests this session. Default: 1900 "
            "(leaves 100 headroom below the 2,000/day cap)."
        ),
    )
    parser.add_argument(
        "--max-per-query",
        type=int,
        default=15,
        metavar="N",
        help=(
            "Stop ingesting from a query after N new tracks and move on to the next "
            "query. Keeps the library diverse across all query terms rather than "
            "downloading every result for a few popular queries. "
            "Default: 15. Set to 0 for no limit (original behaviour)."
        ),
    )
    parser.add_argument(
        "--no-process",
        action="store_true",
        help=(
            "Skip inline MIR processing — only ingest samples into the DB. "
            "Run `python -m scripts.process_queue` in a second terminal to "
            "process them separately."
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Discard saved state and start fresh with a new shuffled query order.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.max_requests, args.max_per_query, args.reset, process_inline=not args.no_process))


if __name__ == "__main__":
    main()
