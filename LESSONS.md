# Debugging Lessons — Audio Sample Manager

Accumulated debugging knowledge for agents and developers working on this codebase.

## Agent Protocol — Read and Write Rules

**Read:** Load this file at the start of every conversation, before writing any code.

**Write:** Update this file autonomously — no need to be asked. Add a new numbered
lesson any time you:
- Root-cause a bug (even one you introduced and fixed yourself)
- Discover a non-obvious behaviour in any library or service used here
- Make an architectural decision that future agents should not re-litigate
- Find a faster or safer way to do something that currently has a worse pattern

**Format:** Add new lessons at the bottom with the next sequential number. Never
renumber existing lessons — CLAUDE.md references them by number. Keep each lesson
self-contained: symptom, root cause, fix, and enough context to apply it without
reading surrounding code.

**Commit:** After any edit, run:
```bash
cd "/mnt/c/Users/joshu/OneDrive/Cooper Union/Databases"
git add LESSONS.md audio-sample-manager/CLAUDE.md
git commit -m "Update LESSONS.md: <brief description of new lesson>"
git push origin main
```

---

## 1. SQLAlchemy Async: PendingRollbackError from DB connection timeout

### Symptom
```
sqlalchemy.exc.PendingRollbackError: Can't reconnect until invalid transaction is
rolled back.  Please rollback() fully before proceeding
```
Often preceded by:
```
asyncpg.exceptions.ConnectionDoesNotExistError: connection was closed in the middle of operation
ConnectionResetError: [Errno 104] Connection reset by peer
```

### Root Cause
A single `async with AsyncSessionLocal() as db:` block was kept open for the full
duration of ML inference (60–120 s). Supabase uses PgBouncer in transaction mode,
which recycles idle connections after ~30 s. When the ML work finished and the code
tried to use `db` again, the underlying connection had already been closed by the
server — leaving the SQLAlchemy session in an invalid state. Any subsequent
`db.execute()` or `db.commit()` raised `PendingRollbackError`.

### Fix: Use Separate Short-Lived Sessions
Never hold a DB session open across slow I/O or CPU-bound work. Break the pipeline
into three sessions:

```python
# Session A: claim entry + fetch URLs (< 1 s)
async with AsyncSessionLocal() as db:
    file_url = ...
    queue_entry_id = ...
# Session A closed — connection returned to pool immediately.

# No session: slow work (download + ML inference, 60–120 s)
audio_bytes = await download(file_url)
features = await loop.run_in_executor(None, extract_features, audio_bytes)

# Session B: write results (< 1 s)
async with AsyncSessionLocal() as db:
    db.add(AudioMetadata(...))
    await db.commit()
```

If the slow work raises, open a **fresh** Session C in the `except` block to record
the failure — do NOT try to reuse the session that may be broken.

### Diagnostics
- Look for `ConnectionResetError` or `ConnectionDoesNotExistError` **before** the
  `PendingRollbackError`. The latter is a consequence, not the root cause.
- Check whether the session was held open across a `loop.run_in_executor()` or
  `asyncio.gather()` call that takes tens of seconds.

### Key Rule
**Session lifetime must be shorter than the database server's idle-connection timeout.**
For Supabase + PgBouncer this is ~30 s. For direct Postgres it is typically 10 min+,
but short sessions are always safer.

---

## 2. TensorFlow Eager Execution: musicnn vs YAMNet Conflict

### Symptom
YAMNet predict silently returns all-zero probabilities, returns no tags, or raises:
```
AttributeError: 'NoneType' object has no attribute ...
```
or the YAMNet model produces garbage outputs even though it loads without error.

### Root Cause
`musicnn/extractor.py` calls `tf.compat.v1.disable_eager_execution()` at **module
import time**. Any code that `import musicnn.tagger` or `import musicnn.extractor`
— even transitively — will call this and disable TF2 eager mode globally in that
process. YAMNet is a TF Hub SavedModel that requires eager execution; once disabled,
YAMNet silently fails.

The disable-eager call is module-level (runs as soon as the module is first
imported), so **order of imports does not help** — importing YAMNet first and
musicnn second still breaks YAMNet on the second musicnn call.

### Fix: Subprocess Isolation
Run musicnn in a separate spawned subprocess using `ProcessPoolExecutor(spawn)`.
The subprocess gets a clean Python interpreter with its own TF state. The main
process never imports musicnn, so its TF eager mode is never disturbed.

```python
import concurrent.futures
import multiprocessing

_executor = concurrent.futures.ProcessPoolExecutor(
    max_workers=1,
    mp_context=multiprocessing.get_context("spawn"),
)

def _predict_subprocess(tmp_path: str, top_k: int) -> list[str]:
    # This runs in the subprocess — disable_eager_execution() stays contained here.
    from musicnn.tagger import top_tags
    return list(top_tags(tmp_path, model="MTT_musicnn", topN=top_k, print_tags=False))

class MusiCNNWorker:
    def predict(self, audio_bytes: bytes, top_k: int = 5) -> list[str]:
        # Write bytes to temp file (subprocess can't receive raw bytes easily)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            future = _executor.submit(_predict_subprocess, tmp_path, top_k)
            return future.result(timeout=300)
        finally:
            os.unlink(tmp_path)
```

**Critical**: Never `import musicnn.tagger` at the top of a module, not even in
a try/except availability check. The import side effect fires immediately.

### Why 'spawn' not 'fork'?
`fork` copies the parent's entire memory state including any partially-initialized
TF or CUDA state, which can cause deadlocks. `spawn` starts a fresh interpreter,
which is safe but slower (must reimport everything). For infrequent ML calls,
`spawn` is the correct choice.

### Diagnostics
If YAMNet returns empty or zero results unexpectedly:
1. Check whether any code path imports `musicnn.tagger`, `musicnn.extractor`,
   or any module that does so transitively.
2. Check `tf.executing_eagerly()` in the main process at the point of YAMNet call.
3. Add `print(tf.executing_eagerly())` before and after any suspicious import.

---

## 3. MusiCNN: UnboundLocalError on Very Short Audio

### Symptom
```
UnboundLocalError: local variable 'batch' referenced before assignment
```
raised inside `musicnn/extractor.py` in `batch_data()`.

### Root Cause
MusiCNN's `batch_data()` function uses a for-loop that builds `batch` frame by
frame. If the audio clip is shorter than one analysis window (3 seconds, or
n_frames=187 at 16 kHz), the loop body never executes and `batch` is never
assigned. The function then tries to return `batch`, raising `UnboundLocalError`.

### Fix
Catch `UnboundLocalError` where `"batch"` is in the message, and return `[]`:

```python
try:
    tags = top_tags(tmp_path, model="MTT_musicnn", topN=top_k, print_tags=False)
    return list(tags)
except UnboundLocalError as exc:
    if "batch" in str(exc):
        return []  # audio too short for analysis
    raise
```

### Note
This fix must live **inside the subprocess function** (`_predict_subprocess`), not
in the main process, because musicnn runs in a subprocess.

---

## 4. asyncio Event Loop and asyncpg Connection Pool Lifetime

### Symptom
```
asyncpg.exceptions._base.InterfaceError: cannot perform operation: another operation is in progress
```
or connections silently fail in a second `asyncio.run()` call in the same process.

### Root Cause
`asyncpg` binds its connection pool to the event loop that was running when the
pool was created. If you call `asyncio.run()` twice (which creates two separate
event loops), the second call gets a new loop but the pool is bound to the first.
All database calls in the second loop fail silently or raise interface errors.

### Fix
Combine all async work into a single top-level `asyncio.run()` call:

```python
async def _main():
    await run_utility(args)   # reset-failed, requeue-done-missing-tags, etc.
    await run_worker(...)     # main processing loop

asyncio.run(_main())
```

Never call `asyncio.run()` twice in the same process — compose coroutines instead.

---

## 5. Librosa: Warning on Very Short Audio (n_fft > signal length)

### Symptom
```
UserWarning: n_fft=1024 is too large for input signal of length=715
```

### Root Cause
Librosa's FFT-based functions (spectral centroid, STFT, etc.) require a signal at
least as long as `n_fft`. For very short audio (< 50 ms), the default `n_fft=2048`
or `n_fft=1024` may exceed the signal length. Librosa issues a warning and pads
the signal internally — results are valid but may be unreliable.

### Behavior
This is a **warning, not an error**. The pipeline continues. The resulting BPM/key
values for extremely short clips may be nonsensical (e.g., BPM=0 or the maximum
tempo detected from silence), but the pipeline does not crash.

### If it needs to be handled
Add a length check before Librosa processing and return a default dict for clips
under some threshold (e.g., < 0.5 s):

```python
y, sr = librosa.load(io.BytesIO(audio_bytes), sr=None)
if len(y) < 512:
    return {"bpm": 0.0, "key": "C", "energy_level": 0.0, ...}
```

---

## 6. ProcessPoolExecutor: BrokenProcessPool Recovery

### Symptom
```
concurrent.futures.process.BrokenProcessPool: A process in the process pool was terminated abruptly
```

### Root Cause
The subprocess running musicnn crashed (OOM, SIGKILL, segfault). Once broken, the
executor cannot submit new tasks and all future `.submit()` calls raise immediately.

### Fix
Catch `BrokenProcessPool`, recreate the executor, and retry once:

```python
try:
    future = executor.submit(_predict_subprocess, tmp_path, top_k)
    return future.result(timeout=300)
except concurrent.futures.process.BrokenProcessPool:
    log.warning("MusiCNN subprocess pool broken — recreating and retrying.")
    _executor = None  # Clear the global so _get_executor() recreates it
    executor = _get_executor()
    future = executor.submit(_predict_subprocess, tmp_path, top_k)
    return future.result(timeout=300)
```

---

## 7. TF_USE_LEGACY_KERAS Environment Variable

### Symptom
```
AttributeError: module 'keras.api._v2.keras' has no attribute 'layers'
```
or
```
ImportError: cannot import name 'Adam' from 'keras.optimizers'
```

### Root Cause
MusiCNN uses `tf.compat.v1.layers` (Keras 2 API). TensorFlow 2.16+ defaults to
Keras 3, which removed these legacy layer APIs.

### Fix
Set the env var **before TF is first imported** in any process that will use
musicnn. In the registry module:

```python
import os
os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
```

And inside the subprocess function:
```python
def _predict_subprocess(tmp_path, top_k):
    import os as _os
    _os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
    from musicnn.tagger import top_tags
    ...
```

`setdefault` is safe — it only sets the variable if it is not already set, so
it won't override a user-set value.

---

## 8. CLAP: Handling Very Short Audio

### Symptom
CLAP `encode_audio` hangs indefinitely or returns a zero vector for audio shorter
than ~1 second.

### Root Cause
LAION-CLAP processes audio in fixed-length windows and may hang or produce
degenerate output for extremely short clips (< ~0.5 s) due to the model's internal
chunking logic.

### Mitigation
The current codebase does not add a minimum-length guard for CLAP. If you see
a sample stuck in `processing` for > 5 minutes, it may be a very short audio file.
The pipeline semaphore (`_pipeline_semaphore`) means a hung CLAP call blocks all
subsequent pipeline runs in that process.

**Future fix**: add a pre-check in `CLAPWorker.encode_audio`:
```python
y, sr = librosa.load(io.BytesIO(audio_bytes), sr=48_000, mono=True)
if len(y) < 4800:  # < 0.1 s at 48 kHz
    return [0.0] * 512
```

---

## 9. Supabase / PgBouncer Connection Behavior

### Key facts
- Supabase's managed Postgres uses PgBouncer in **transaction pooling** mode.
- In transaction mode, a physical connection is only held during a transaction.
  Between transactions, the connection is returned to the pool.
- **Idle timeout**: PgBouncer closes connections idle for ~30 s server-side.
  A SQLAlchemy session object can survive this (it's a Python object), but the
  underlying asyncpg connection is gone. The next statement on that session will
  get `ConnectionDoesNotExistError`.
- **asyncpg reconnect**: asyncpg does NOT automatically reconnect on a closed
  connection mid-transaction. Once `ConnectionDoesNotExistError` is raised, the
  session is in an invalid state and must be discarded (the `async with` context
  manager handles this on `__aexit__`).

### Operational rules
1. Keep session lifetimes short (< 10 s in all normal cases).
2. Never `await` long operations inside an open session.
3. Use `expire_on_commit=False` (already set in `database.py`) so that ORM
   objects remain accessible after `await db.commit()` without triggering
   lazy-load I/O.

---

## 10. SQLAlchemy: expire_on_commit=False is Required for Async

### Why
In SQLAlchemy's async mode, expired attributes cannot be lazy-loaded (lazy load
would require implicit I/O which is not allowed in async context). After
`await db.commit()`, all ORM objects are expired by default. Any attribute access
on an expired object outside an open session raises `MissingGreenlet` or
`DetachedInstanceError`.

### Fix (already applied in `database.py`)
```python
AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,  # prevents expired-attribute errors after commit
    class_=AsyncSession,
)
```

### Corollary
With `expire_on_commit=False`, ORM objects reflect the **pre-commit state** after
a commit. If you need fresh data after a commit (e.g., server-generated values),
call `await db.refresh(obj)` explicitly.

---

## 11. pgvector: Two-Query Pattern for Vector Search with ORM Relationships

### Why a second query is needed
pgvector's `<=>` (cosine distance) operator returns raw `Row` objects, not ORM
instances. You can't call `selectinload` on raw rows. To get ORM objects with
eager-loaded relationships (audio_metadata, tags), you need a second query.

### Pattern (used in `search.py: _vector_search`)
```python
# Query 1: fast vector search — returns ordered UUIDs
raw = await db.execute(
    text("""
        SELECT sample_id, embedding <=> :vec AS distance
        FROM audio_embeddings
        ORDER BY distance
        LIMIT :k
    """),
    {"vec": str(embedding_list), "k": limit},
)
rows = raw.fetchall()
ordered_ids = [r.sample_id for r in rows]
distance_map = {r.sample_id: r.distance for r in rows}

# Query 2: ORM query with selectinload to get full objects
result = await db.execute(
    select(Sample)
    .options(selectinload(Sample.audio_metadata), selectinload(Sample.tags))
    .where(Sample.id.in_(ordered_ids))
)
samples = result.scalars().all()

# Re-sort by original distance order
samples.sort(key=lambda s: distance_map[s.id])
```

---

## 12. process_queue.py: Claiming Items and Retry Logic

### Atomic claim pattern
Use `SELECT … FOR UPDATE SKIP LOCKED` to atomically claim a pending item without
blocking other workers (each worker picks a different row):

```python
result = await db.execute(
    select(ProcessingQueue)
    .where(ProcessingQueue.status == ProcessingStatus.pending)
    .order_by(ProcessingQueue.created_at)
    .limit(1)
    .with_for_update(skip_locked=True)
)
entry = result.scalar_one_or_none()
```

### Stale detection
A worker crash leaves entries stuck in `processing`. Reset them with:
```python
update(ProcessingQueue)
.where(
    ProcessingQueue.status == ProcessingStatus.processing,
    ProcessingQueue.updated_at < stale_cutoff,
)
.values(status=ProcessingStatus.pending, worker_id=None, ...)
```

### The claimed=True flag
When `process_queue.py` claims an entry, it sets `status='processing'` via SKIP
LOCKED. It then passes `claimed=True` to `_run_mir_pipeline`. Without this flag,
`_run_mir_pipeline` would try to do its own atomic claim (looking for
`status='pending'`), find nothing (already `'processing'`), and bail out silently.

---

## 13. Background Tasks vs. Queue Worker: Avoiding Double-Processing

`POST /api/samples/` registers a `BackgroundTask` that calls
`_run_mir_pipeline(sample_id)` (with `claimed=False`). If `process_queue.py` runs
concurrently and claims the same entry first, the background task's atomic claim
attempt finds `status != 'pending'` and exits silently. No double-processing occurs.
The `SELECT FOR UPDATE SKIP LOCKED` + `UPDATE WHERE status='pending'` pattern is the
key — it is atomic at the database level across all processes.

---

## 14. Checking Pipeline Status

### Via API (preferred)
```
GET http://localhost:8000/api/admin/queue
```
Returns JSON with counts per status, percent done, and recent failure details.

### Via Python
```python
import asyncio
from app.database import AsyncSessionLocal
from app.models.system import ProcessingQueue, ProcessingStatus
from sqlalchemy import select, func

async def check():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ProcessingQueue.status, func.count().label('c'))
            .group_by(ProcessingQueue.status)
        )
        for status, count in result.all():
            print(f'{status.value}: {count}')

asyncio.run(check())
```

### Recovering failed samples
```bash
# Reset all failed entries to pending (retry from scratch)
python -m scripts.process_queue --reset-failed

# Re-process done samples that have no YAMNet/MusiCNN tags
python -m scripts.process_queue --requeue-done-missing-tags

# Combine: reset and immediately start processing
python -m scripts.process_queue --reset-failed --requeue-done-missing-tags --once
```

---

## 15. When a Sample Is Stuck in 'processing' Forever

### Causes
1. Worker process crashed (SIGKILL, OOM) mid-pipeline.
2. CLAP hung on very short audio.
3. Network timeout not raised (unlikely given httpx timeout=60 s).

### Fix
The `--stale-minutes` mechanism resets these automatically:
- `process_queue.py` resets any `processing` entry whose `updated_at` is older
  than `--stale-minutes` (default 15) back to `pending`.
- A manually stuck entry can be reset with a direct DB update or by running
  `python -m scripts.process_queue --reset-failed` (which also resets failed).

Actually `--reset-failed` only resets `failed`. To manually reset a stuck
`processing` entry:
```python
await db.execute(
    update(ProcessingQueue)
    .where(ProcessingQueue.status == ProcessingStatus.processing)
    .values(status=ProcessingStatus.pending, worker_id=None,
            updated_at=datetime.now(timezone.utc))
)
await db.commit()
```

---

## 16. General Async SQLAlchemy Patterns to Avoid

| Anti-pattern | Problem | Fix |
|---|---|---|
| Access ORM attribute after session closed | `DetachedInstanceError` | Keep session open or use `expire_on_commit=False` |
| `db.execute()` inside `except` without rollback | `PendingRollbackError` | Open a fresh session in `except` |
| `asyncio.run()` called twice in same process | asyncpg pool bound to dead loop | Use a single `asyncio.run(_main())` |
| Hold session open during `run_in_executor()` | Connection timeout after 30 s | Close session before slow work |
| `selectinload` after pgvector raw query | `MissingGreenlet` | Use two-query pattern (see lesson 11) |
| `import musicnn.tagger` in main process | Disables TF eager globally | Use subprocess isolation (see lesson 2) |

---

## 17. asyncpg: Always Pass datetime Objects for TIMESTAMPTZ Parameters

### Symptom
```
asyncpg.exceptions.DataError: invalid input for query argument $1:
'2026-03-17' (expected a datetime.date or datetime.datetime instance, got 'str')
```

### Root Cause
asyncpg strictly type-checks bind parameters for `TIMESTAMPTZ` columns. Passing a
plain string like `'2026-03-17'` that works in raw psql fails in asyncpg.

### Fix
Always construct a proper `datetime` object with timezone:
```python
from datetime import datetime, timezone

cutoff = datetime(2026, 3, 17, 0, 0, 0, tzinfo=timezone.utc)
await db.execute(text('SELECT ... WHERE created_at < :c'), {'c': cutoff})
```

This applies to both `text()` raw SQL and ORM queries. String dates are never safe
as bind parameters to asyncpg regardless of how obvious the value looks.

---

## 18. Storage Pruning: Collect URLs Before Deleting DB Rows

### Rule
When deleting samples that have associated files in Supabase Storage, always
collect the `file_url` values from the DB **before** deleting the rows.
Once the DB row is gone, the storage path is unrecoverable.

### Pattern
```python
# Step 1: collect URLs while rows still exist
result = await db.execute(
    text('SELECT file_url FROM samples WHERE created_at < :c'),
    {'c': cutoff}
)
urls = [row[0] for row in result.all()]

# Step 2: delete from storage
paths = [url.replace(BASE_URL_PREFIX, '').rstrip('?') for url in urls]
supabase.storage.from_(bucket).remove(paths)  # batch delete

# Step 3: delete from DB (cascades handle child rows)
await db.execute(text('DELETE FROM samples WHERE created_at < :c'), {'c': cutoff})
await db.commit()
```

### Batch size
Supabase Storage's `remove()` handles lists well. Use batches of 100 to avoid
request size limits:
```python
for i in range(0, len(paths), 100):
    supabase.storage.from_(bucket).remove(paths[i:i+100])
```

### Cascade safety
All FK relationships on the `samples` table use `ON DELETE CASCADE`:
`audio_embeddings`, `audio_metadata`, `sample_tags`, `processing_queue`,
`comments`, `ratings`, `download_history`, `collection_items`, `pack_samples`.
Deleting from `samples` is sufficient — no need to manually delete child rows.

### Orphaned tags
`tags` rows are shared across samples and do NOT cascade. After a bulk delete,
clean orphaned tags separately:
```python
await db.execute(text(
    'DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM sample_tags)'
))
await db.commit()
```

---

## 19. Supabase Storage: Free Tier Limits and Misleading file_size_bytes

### Storage limit
Supabase free tier: **1 GB** of Storage. At ~150–300 KB per MP3 preview, that's
roughly 3,000–6,000 tracks. Monitor usage in the Supabase dashboard under Storage.

### file_size_bytes is the Freesound ORIGINAL file size, not the stored preview
The `samples.file_size_bytes` column is populated from Freesound's `filesize` field,
which refers to the full-quality original file (often 10–50 MB WAV/FLAC). The actual
stored file is the HQ MP3 **preview** (~150–300 KB). Do not use `SUM(file_size_bytes)`
to estimate Supabase Storage usage — it will be orders of magnitude too high.

### Estimating actual storage
Use: `num_samples × ~200 KB` as a rough estimate, or check the Supabase dashboard
directly for ground truth.

### Storage URL format
```
https://<project>.supabase.co/storage/v1/object/public/audio-previews/freesound/12345.mp3?
```
Storage path (for deletion): `freesound/12345.mp3`
Strip the base URL prefix and trailing `?`.

---

## 20. Running the Worker: Use a User-Owned Terminal

### Problem
Starting `process_queue` as a background nohup process (from Claude Code's bash tool
or a script) means it is invisible — you cannot see its output, you won't know if it
crashes, and you have no easy way to stop it cleanly.

### Rule
Always run `process_queue` in a terminal you own:
```bash
cd audio-sample-manager
source .venv/bin/activate
python -m scripts.process_queue
```

Check it is running:
```bash
ps aux | grep process_queue | grep -v grep
```

If that returns nothing, the worker is dead and needs a manual restart.

### Why workers die silently
The most common causes are:
- OOM (CLAP + YAMNet + MusiCNN together use ~3 GB RAM; other processes can crowd this)
- SIGKILL from the OS OOM killer
- Unhandled exception that bypasses the retry logic

The stale-detection mechanism in `process_queue.py` (`--stale-minutes`, default 15)
resets any `processing` entry whose `updated_at` is older than the cutoff back to
`pending`, so a crashed worker does not permanently block those samples.

---

## 21. Bulk Ingestion Without Per-Query Cap Causes Storage Bloat

### What happened
Running `ingest_overnight.py` without `--max-per-query` downloaded every result for
every query (up to 150 per page × multiple pages). Popular queries like "violin" or
"kick drum" returned hundreds of nearly identical results, filling Supabase Storage
with redundant tracks.

### Fix (already in place)
`--max-per-query` (default 15) stops ingesting from a query after N new tracks and
moves to the next. 300 queries × 15 = ~4,500 tracks max ≈ ~700 MB.

```bash
python -m scripts.ingest_overnight --no-process               # default 15/query
python -m scripts.ingest_overnight --no-process --max-per-query 10  # more conservative
python -m scripts.ingest_overnight --no-process --max-per-query 0   # no cap (danger)
```

### Recovery: pruning over-ingested tracks
If storage is exceeded, prune by date (tracks ingested before the cap was active are
the most redundant). See lesson 18 for the deletion pattern. Key steps:
1. Kill the worker
2. Collect `file_url` for rows to delete
3. Delete from Supabase Storage in batches of 100
4. `DELETE FROM samples WHERE <condition>` — cascades handle everything
5. `DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM sample_tags)`
6. Restart the worker in a user-owned terminal


---

## 22. Deployment Architecture: ML Models vs. Cloud RAM Limits

### The Problem
CLAP (~900 MB weights), YAMNet, and MusiCNN together require ~3 GB RAM at runtime.
Railway free tier provides 512 MB. You cannot load all three ML models on Railway's
free tier — the process will OOM-kill before serving any requests.

### Recommended Architecture (hybrid cloud/local)
- **Railway** — hosts FastAPI (`uvicorn app.main:app`). Loads CLAP only (needed for
  search text/audio encoding). Does NOT run `process_queue`.
- **Vercel** — hosts the React/Vite frontend as a static build.
- **Supabase** — Postgres DB + Storage (already cloud, no change).
- **Local machine** — runs `process_queue` worker. Has direct access to Supabase DB
  and Storage over the internet. This is a legitimate hybrid architecture.

This is acceptable because the worker is a background batch job, not a user-facing
service. Producers get search results from Railway; samples get processed from the
local machine. Both read/write the same Supabase DB.

### If Railway free tier OOMs on CLAP
Options in order of preference:
1. Upgrade to Railway Starter ($5/mo, 8 GB RAM) — CLAP loads fine.
2. Lazy-load CLAP (only on first search request) — may still OOM depending on
   baseline memory usage from FastAPI + asyncpg + other imports.
3. Disable the search endpoint on Railway and document it as a known limitation.
   Search still works when running locally.

### Required changes before deploying
1. **CORS** — add the Vercel production URL to `allow_origins` in `app/main.py`.
   Currently only `http://localhost:5173` is allowed.
2. **Frontend API URL** — set `VITE_API_URL` in Vercel env to the Railway backend
   URL. The Vite dev proxy (`localhost:8000`) only works locally.
3. **Railway env vars** — set all vars from `.env` in Railway's dashboard:
   `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`,
   `SUPABASE_STORAGE_BUCKET`, `SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`.
   Never commit `.env` to git.
4. **`--workers 1`** on Railway — ML worker singletons are not safe across forked
   processes. Use a single uvicorn worker.

### Demo social data (seed before presentation)
Comments, ratings, and collections all have 0 rows. Before the demo:
- Create a second user account via `POST /api/auth/register`
- Rate several samples via `POST /api/samples/{id}/ratings`
- Leave comments via `POST /api/samples/{id}/comments`
- Create a collection and add samples via the collections endpoints
This takes ~10 minutes via Swagger UI (`http://localhost:8000/docs`) but is
important for showing the social schema is actually being used.

---

## 23. Google Drive: Service Account Quota vs. Personal Google One Quota

### The Non-Obvious Distinction
A Google service account has its **own separate Drive storage quota (15 GB free)**,
completely independent of any personal Google account. Files uploaded by a service
account are owned by that service account and count against its quota — not yours —
even if the destination folder is shared with your personal account.

To use your personal **Google One** storage (1 TB), you must authenticate as
yourself via **OAuth2**, not a service account.

### Architecture Decision
`app/services/gdrive.py` was migrated from service account credentials to OAuth2
with a stored refresh token. The credentials required in `.env` are:

```
GDRIVE_CLIENT_ID       — "Desktop app" OAuth2 client from Cloud Console (free)
GDRIVE_CLIENT_SECRET   — from Cloud Console
GDRIVE_REFRESH_TOKEN   — generated once by scripts/gdrive_auth.py
GDRIVE_FOLDER_ID       — Drive folder ID (unchanged)
```

No JSON key file is needed for either local dev or Railway deployment.

### OAuth2 Refresh Token Lifecycle
- Access tokens expire after **1 hour** — the `google-api-python-client` refreshes
  them automatically using the refresh token. No manual intervention needed.
- The refresh token itself **does not expire** unless:
  - Unused for more than 6 consecutive months, or
  - Manually revoked via Google Account → Security → Third-party access.
- The `_service()` singleton calls `creds.refresh(Request())` eagerly at startup to
  surface misconfiguration before the first real upload attempt.

### One-Time Setup
Run `scripts/gdrive_auth.py` once per environment (local, Railway):
```bash
python -m scripts.gdrive_auth --client-id ID --client-secret SECRET
```
For WSL2: the script starts a local server on port 8080; paste the printed URL into
your Windows browser. If localhost redirect fails, re-run with `--no-server` for the
manual copy-paste flow.

### OAuth Consent Screen: Test Users
While the Google Cloud project is in "Testing" mode, only explicitly listed test
users can authorize the app. In Cloud Console → Google Auth Platform → Audience →
Test users, add the Gmail account that owns your Google One plan. Without this step
the OAuth flow returns `Error 403: access_denied`.

---

## 24. pip Resolver: laion-clap numpy Pin Conflict with TensorFlow 2.20

### Symptom
```
ERROR: Cannot install ... laion-clap==1.1.4 and numpy<2.0 and >=1.26.0 because
these package versions have conflicting dependencies.
The conflict is caused by: laion-clap 1.1.4 depends on numpy==1.23.5
```
`pip install -r requirements.txt` fails entirely even though all packages are
already installed and the app runs fine.

### Root Cause
`laion-clap==1.1.4` published its PyPI metadata with a **strict equality pin**
`numpy==1.23.5`. TensorFlow 2.20 requires `numpy>=1.26.0`. pip's resolver sees
these as irreconcilable and aborts, even though numpy 1.26.x works fine with
laion-clap at runtime (the pin is overly conservative).

### Fix
Upgrade to `laion-clap==1.1.7`, which relaxed the constraint to `numpy>=1.23.5,<2.0`.
This is compatible with TF 2.20's `>=1.26.0` requirement.

The public APIs used by this project (`CLAP_Module`, `get_text_embedding`,
`get_audio_embedding_from_filelist`, `clap_module.factory.load_state_dict`) are
unchanged across 1.1.4 → 1.1.7.

### install.sh
A second package, `musicnn==0.1.0`, also has an incompatible numpy pin but is
handled differently — it must be installed with `--no-deps` (see lesson 2, 3).
`pip install -r requirements.txt` does not support per-line `--no-deps`, so
`install.sh` at the repo root handles the full install sequence:
```bash
bash install.sh
# equivalent to:
pip install -r requirements.txt           # resolves cleanly with laion-clap 1.1.7
pip install --no-deps musicnn==0.1.0      # bypasses musicnn's numpy pin
```

---

## 25. MusiCNN Subprocess: Importing librosa Before TF Causes libprotobuf Segfault

### Symptom
```
python[NNNN]: segfault at 0 ip 0x... sp 0x... error 4 in libprotobuf.so.25.3.0
concurrent.futures.process.BrokenProcessPool: A process in the process pool was
terminated abruptly while the future was running or pending.
```
The musicnn subprocess crashes EVERY TIME, making all queue entries fail.

### Root Cause
In a `spawn`-context subprocess, import order matters for native shared libraries.
If `import librosa` (which pulls in numpy C extensions, scipy, soundfile, etc.)
runs **before** `from musicnn.tagger import top_tags` (which initialises TensorFlow),
the shared-library loader binds `libprotobuf.so` to the version brought in by
librosa's dependency chain. TF then tries to use its own incompatible protobuf API
against that version → segfault at TF initialisation time.

This is deterministic: it crashes on the very first subprocess call, 100% of the
time. No amount of retry helps.

### Confirmed by
```
dmesg: python[NNNN]: segfault at 0 ip ... in libprotobuf.so.25.3.0
```
(Two separate subprocess attempts, same crash address — deterministic library
conflict, not a race or memory issue.)

### Fix: Duration Guard Lives in the Parent Process
Do NOT import librosa inside `_predict_subprocess`. Keep the subprocess as clean as
possible — only `os.environ` setup + `musicnn.tagger`. Move the duration check to
`MusiCNNWorker.predict()` in the parent process, using `soundfile` (already
installed as a librosa transitive dependency) to read the audio header from bytes:

```python
# In MusiCNNWorker.predict() — parent process, librosa already loaded safely:
import soundfile as sf
import io
try:
    with sf.SoundFile(io.BytesIO(audio_bytes)) as f:
        duration = len(f) / f.samplerate
    if duration < 3.0:
        return []
except Exception:
    pass  # unknown format — let musicnn try

# Then write temp file and submit to subprocess as before.
```

```python
# In _predict_subprocess — subprocess, must stay TF-only:
def _predict_subprocess(tmp_path: str, top_k: int) -> list[str]:
    import os as _os
    _os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
    # NO librosa here — see above
    from musicnn.tagger import top_tags
    ...
```

### Generalisation
**Never import any library that loads numpy/scipy native extensions before
TensorFlow in a spawned subprocess.** The shared-library initialisation order in
`spawn` processes is different from the parent and can bind the wrong version of
TF's bundled C libraries (protobuf, abseil, etc.).

### Applies to
`app/workers/musicnn_worker.py`

---

## 27. MusiCNN Subprocess: session.run() Crash — Root Cause: Anaconda libprotobuf Conflict

### Symptom
```
I0000 ... mlir_graph_optimization_pass.cc:437] MLIR V1 optimization pass is not enabled
Fatal Python error: Segmentation fault
  File "tensorflow/python/client/session.py", line 1483 in _call_tf_sessionrun
  ...
  File "musicnn/tagger.py", line 60 in top_tags
```
The musicnn subprocess crashes on every sample, right after the MLIR log line,
during TF's first `session.run()` call. Both the initial attempt and the
`BrokenProcessPool` retry fail.

### Root Cause: Anaconda libprotobuf ABI conflict

When the subprocess runs with **Anaconda's Python** (`~/anaconda3/bin/python3.12`),
the dynamic linker finds `~/anaconda3/lib/libprotobuf.so.25.3.0` on the library
search path. TF 2.20 was compiled against a DIFFERENT build of `libprotobuf.so.25.3.0`
(the PyPI wheel builder's build). Although both files carry the same soname version
`25.3.0`, they have different internal struct layouts (different compiler flags, inline
thresholds, or Abseil dependency versions). When `sess.run()` tries to serialise
`RunOptions` through the Anaconda build of libprotobuf, a required vtable pointer or
internal struct member is NULL → SIGSEGV at offset `0x16d5fd`.

Confirmed with `faulthandler`:
```
Extension modules loaded before crash: numpy.core._multiarray_umath, ...,
  google._upb._message, scipy.*, pyarrow.lib, pandas.*, numba.*, ...
```
`ctypes.util.find_library('protobuf')` returns `~/anaconda3/lib/libprotobuf.so.25.3.0`.

The **system Python** (`/usr/bin/python3`) does not have `~/anaconda3/lib` on its
dynamic-linker search path, so TF uses the correct system libprotobuf and succeeds.
Confirmed: `/usr/bin/python3 -c "from musicnn.tagger import top_tags; top_tags(...)"`
works perfectly on the same samples that crash under the Anaconda interpreter.

### Fix: Use /usr/bin/python3 for the musicnn subprocess

Replace the `ProcessPoolExecutor` (which inherits `sys.executable` = Anaconda Python)
with a **persistent `/usr/bin/python3` subprocess** communicating over stdin/stdout
JSON IPC:

- `app/workers/_musicnn_proc.py` — the worker script; loads TF once, then loops
  reading `{"path":..., "top_k":...}` from stdin and writing `{"tags":[...]}` to stdout.
- `app/workers/musicnn_worker.py` — starts `_musicnn_proc.py` via
  `subprocess.Popen(['/usr/bin/python3', ...])`, manages lifecycle (restart on crash),
  and applies a per-call timeout via a background reader thread.

TF and the MTT_musicnn checkpoint load once at subprocess start (~3–5 s cold start).
Subsequent calls are fast (~1–2 s each). The subprocess restarts automatically if it
dies; after two consecutive failures predict() returns [] so the rest of the pipeline
(CLAP, YAMNet, Librosa) can still complete.

### Lesson
If you have Anaconda installed alongside pip TF, spawned subprocesses using Anaconda's
Python will inherit its `lib/` directory on the dynamic-linker search path. This can
silently substitute Anaconda's builds of shared libraries (protobuf, absl, etc.) for
the ones TF was compiled against — even when both have the same soname version. Always
use `/usr/bin/python3` (or a clean virtual environment) for subprocesses that load TF.

---

## 28. Railway Deployment: CPU-Only PyTorch is Non-Negotiable

### Problem
`torch==2.3.0` on PyPI defaults to the CUDA wheel (~2.5 GB). On a Railway Hobby
instance (CPU-only) this either times out the build step or consumes unnecessary
disk space. The CUDA wheel installs fine but adds 2–3 minutes to every deploy.

### Fix
Use the PyTorch CPU wheel index in `requirements-railway.txt`:
```
--extra-index-url https://download.pytorch.org/whl/cpu
torch==2.3.0+cpu
torchvision==0.18.0+cpu
```
And override Nixpacks' default install step via `nixpacks.toml`:
```toml
[phases.install]
cmds = ["pip install -r requirements-railway.txt"]
```
This reduces the PyTorch install from ~2.5 GB to ~175 MB.

### What Railway needs vs. what stays local
Railway only runs the FastAPI server + CLAP (for text/audio search). YAMNet,
MusiCNN, and `process_queue` all run locally. `requirements-railway.txt` omits
TF entirely — its workers never load on Railway.

---

## 29. User File Upload: Run gdrive.upload_audio() in a Thread Executor

### Problem
`gdrive.upload_audio()` uses `google-api-python-client` which is synchronous.
Calling it directly inside an `async def` handler blocks the uvicorn event loop
for the entire upload duration (Drive API call can take 1–5 seconds).

### Fix
Run the blocking Drive call in a thread pool:
```python
loop = asyncio.get_running_loop()
gdrive_file_id, public_url = await loop.run_in_executor(
    None, gdrive.upload_audio, audio_bytes, drive_filename, mime
)
```
This frees the event loop to serve other requests while the Drive upload
completes in a background thread. Same pattern as the existing `run_in_executor`
calls for CLAP and Librosa in `_run_mir_pipeline`.

---

## 30. FastAPI File Upload: Validate Extension, Not Content-Type

### Problem
Browsers send inconsistent `Content-Type` headers for audio files:
- Chrome: `audio/mpeg` for MP3
- Safari: `audio/x-m4a` for M4A
- Some: `application/octet-stream` for anything

### Fix
Validate by file extension, not content type header:
```python
ext = Path(file.filename or "").suffix.lower()
if ext not in _ALLOWED_EXTENSIONS:
    raise HTTPException(status_code=415, detail=...)
```
Use a `_MIME_BY_EXT` dict to determine the Drive mimetype from the extension,
ignoring what the client claims the content-type is.

---

## 31. Railway CORS: Make Origins Configurable via Env Var

### Problem
Hardcoding CORS origins in `app/main.py` means you need a code change + redeploy
every time you add a new allowed origin (e.g. when you get the Vercel URL).

### Fix
Store origins as a comma-separated env var `ALLOWED_ORIGINS` and parse at startup:
```python
_origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, ...)
```
Set `ALLOWED_ORIGINS` in Railway's dashboard Variables tab. Changing it there
triggers a redeploy without touching code.

### Two-pass deploy order
1. Deploy Railway backend with `ALLOWED_ORIGINS=http://localhost:5173`
2. Deploy Vercel frontend → get the `.vercel.app` URL
3. Update Railway's `ALLOWED_ORIGINS` to include the Vercel URL → auto-redeploy

---

## 26. OAuth2 Login Form: Pass Email Not Username as the `username` Field

### Symptom
After a successful `POST /api/auth/register`, the auto-login step fails with 401
or "incorrect username or password", even though the user was just created.

### Root Cause
`POST /api/auth/token` is a standard OAuth2 password form endpoint. FastAPI's
`OAuth2PasswordRequestForm` exposes the credential as a field named `username`, but
the backend implementation looks up the user by **email**, not by `username` column.
The frontend's `authStore.register()` was passing `username` (the display name) to
`apiLogin()` as the credential — which the backend could not find.

### Fix
In `frontend/src/store/authStore.ts`, pass `email` (not `username`) to `apiLogin()`
after registration:

```ts
// Before (broken):
const data = await apiLogin(username, password);

// After (correct):
const data = await apiLogin(email, password);
```

Also update `frontend/src/pages/LoginPage.tsx` to:
- Change the field label from "Username" to "Email"
- Add `type="email"` to the input for browser validation and autofill hints

### Rule
Whenever the backend `/auth/token` handler does `user = get_by_email(form.username)`,
the frontend **must** send email — even though the OAuth2 spec calls the field
`username`. Document this mismatch prominently at the auth layer.

---

## 32. `railway up` from WSL/OneDrive Uploads Pyc Files → Null-Byte Crash

### Symptom
After `railway up` from a WSL path under `/mnt/c/…` (OneDrive), Railway crashes
immediately on startup with:
```
SyntaxError: source code string cannot contain null bytes
File "/app/app/main.py", line 6, in <module>
    from app.database import get_db
```

### Root Cause
`railway up` bundles files from the local filesystem and does **not** honour
`.gitignore`. The `app/__pycache__/*.pyc` bytecode files are included alongside
the `.py` sources. On Railway, Python finds (e.g.) `database.cpython-311.pyc`
next to `database.py` and tries to exec the binary as source text, producing
the null-bytes error.

### Fix
1. Add a `.railwayignore` (already committed) to exclude `__pycache__/` and
   `*.pyc` from CLI uploads.
2. **Preferred**: connect Railway to the GitHub repository and deploy via
   `git push origin main` instead of `railway up`. GitHub-based deploys pull
   only committed files, bypassing the local filesystem entirely.

### Rule
Never use `railway up` from a WSL path that passes through Windows NTFS or
OneDrive. Always use GitHub-connected deploys.

---

## 33. VITE_API_URL Is a Build-Time Variable — Missing It Silently Falls Back to localhost

### Symptom
Vercel-hosted frontend shows no samples and Railway logs show zero incoming
`/api/` requests. Vercel's own deployment preview screenshot shows samples;
regular browser visits do not.

### Root Cause
`VITE_API_URL` is **baked into the JS bundle at compile time** by Vite:

```ts
const BASE_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
```

If the variable is absent when `npm run build` runs, the bundle is compiled with
`http://localhost:8000` hardcoded. Setting it in Vercel's dashboard after the
build has zero effect until the project is rebuilt.

Vercel's preview screenshot is generated by Vercel's own authenticated system
and may bypass deployment protection or resolve Railway differently. Regular
browser visits get connection-refused on `localhost:8000` with no visible error.

Confirm by fetching the deployed JS bundle and grepping for URLs:
```bash
curl -s https://your-app.vercel.app/assets/index-XXXX.js \
  | grep -oP 'https?://[a-z0-9._-]+(?::\d+)?' | sort -u
```
If `localhost:8000` appears, the env var was missing at build time.

### Fix
Commit `frontend/.env.production` with the Railway URL. Vite reads this
automatically during production builds — no Vercel dashboard configuration
required, and it survives future redeploys:
```
VITE_API_URL=https://audio-sample-manager-production.up.railway.app
```
`frontend/.env.production` is intentionally NOT in `.gitignore`
(only `frontend/.env` and `frontend/.env.local` are excluded).

---

## 34. Vercel Deployment Protection Blocks All Public Visitors

### Symptom
Visiting the Vercel URL redirects to `vercel.com/sso-api` with
"Authentication Required". Vercel's dashboard previews work because they are
authenticated as the team owner.

### Root Cause
Vercel enables **Deployment Protection** by default on some plans. When enabled,
all visitors must authenticate with a Vercel account.

### Fix
Vercel dashboard → Project → **Settings → Deployment Protection** → **None**.

Must be disabled before the final presentation — the course requires a publicly
accessible URL.

---

## 35. Railway ALLOWED_ORIGINS: Line Break in Dashboard Field Silently Breaks CORS

### Symptom
CORS preflight returns `400 Disallowed CORS origin` even after setting
`ALLOWED_ORIGINS` in Railway's Variables tab to the correct Vercel URL. The
Starlette middleware is running but not matching the origin.

### Root Cause
When pasting a long comma-separated value into Railway's Variables UI, the text
can wrap visually or a newline can be inadvertently inserted. The stored value
becomes e.g.:
```
http://localhost:5173,https://your-app.vercel\n  .app
```
`settings.ALLOWED_ORIGINS.split(",")` splits correctly on the comma, but
`.strip()` only removes leading/trailing whitespace — the newline embedded in
the middle of the URL survives and the origin never matches.

### Diagnosis
The `content-length: 22` on the 400 OPTIONS response confirms Starlette is
returning `"Disallowed CORS origin"` (exactly 22 bytes). The middleware is
running; the URL simply doesn't match.

### Fix
Re-type (don't paste) the value in Railway's Variables field as a single line:
```
http://localhost:5173,https://audio-sample-manager-6ouyzyyoo-josh-miao-s-projects.vercel.app
```
No spaces around the comma, no line breaks anywhere in the string.


---

## 36. `DuplicatePreparedStatementError` in `ingest_overnight.py` — PgBouncer + Concurrent asyncio.gather

### Symptom
```
asyncpg.exceptions.DuplicatePreparedStatementError: prepared statement "__asyncpg_stmt_1__" already exists
[SQL: select pg_catalog.version()]
```
Raised during `scripts/ingest_overnight.py` when `--process` (inline MIR) is
active. Only occurs when `_producer` and `_consumer` run concurrently via
`asyncio.gather`.

### Root Cause
`asyncio.gather(_producer(...), _consumer(...))` schedules both coroutines
concurrently. Each makes a database call shortly after startup, causing
SQLAlchemy to create two pool connections at nearly the same time. Both
connections go through Supabase's PgBouncer pooler (transaction mode). PgBouncer
recycles connections, so it can route both to the same underlying PostgreSQL
backend connection. SQLAlchemy's asyncpg dialect runs `select pg_catalog.version()`
via a named prepared statement (`__asyncpg_stmt_1__`) during its per-connection
initialization. When two connections share the same backend connection, the second
`PREPARE __asyncpg_stmt_1__` fails because the first already created it.

`statement_cache_size=0` in `connect_args` disables asyncpg's LRU cache, but the
dialect initialization still goes through `_prepare_and_execute` with a named
statement — it does not use the simple query protocol. So the error persists.

### Fix
Pre-warm one pool connection **before** `asyncio.gather`. This forces dialect
initialization (the `select pg_catalog.version()` round-trip) to complete
single-threaded. By the time `asyncio.gather` starts both tasks, the engine is
already initialized and subsequent connections skip the version check entirely:

```python
# In ingest_overnight.py run(), just before asyncio.gather:
from app.database import engine   # already imported at module level now

async with engine.connect() as _conn:
    pass  # triggers dialect init single-threaded; subsequent connections skip it

await asyncio.gather(_producer(...), _consumer(...))
```

Also add `engine` to the module-level import: `from app.database import AsyncSessionLocal, engine`.

### Applied to
`scripts/ingest_overnight.py` — `run()` function, immediately before the
`asyncio.gather` call inside the `if process_inline:` branch.

---

## 37. asyncpg + PgBouncer Transaction Mode: statement_cache_size=0 Is Not Enough

### Symptom
`DuplicatePreparedStatementError: prepared statement "__asyncpg_stmt_9__" already exists`
(or `InvalidSQLStatementNameError: ... does not exist`) when running two concurrent
`asyncio.gather` tasks that both open SQLAlchemy `AsyncSession` connections through
Supabase's PgBouncer pooler (port 6543).  Occurs even though `statement_cache_size=0`
is set in `connect_args`.

### Root Cause
`statement_cache_size=0` disables asyncpg's *LRU cache* but does **not** prevent
asyncpg from creating **named** prepared statements.  Every SQLAlchemy ORM query still
calls `asyncpg.connection.prepare()`, which generates a name like `__asyncpg_stmt_N__`
where N is a per-connection hex counter starting from 1.

Two concurrent connections (e.g. producer + consumer in `asyncio.gather`) each start
their counter at 1.  When both reach counter N=9 at the same moment, PgBouncer
(transaction mode) can route both to the same PostgreSQL backend connection.  Both
send `PREPARE __asyncpg_stmt_9__` → duplicate error.

`db.refresh(sample)` after `db.commit()` triggers a SELECT that crosses a PgBouncer
transaction boundary (backend was returned to pool on COMMIT), producing the inverse
error: `InvalidSQLStatementNameError: ... does not exist`.

### Fix
Bypass PgBouncer for scripts by connecting directly to PostgreSQL (port 5432 instead
of 6543).  Each asyncpg connection gets a dedicated backend → no naming conflict.
Use `NullPool` so SQLAlchemy doesn't add its own connection layer on top.

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool
from app.config import settings

url = settings.DATABASE_URL.replace(":6543/", ":5432/")
direct_engine = create_async_engine(url, poolclass=NullPool,
                                    connect_args={"statement_cache_size": 0})
DirectSession = async_sessionmaker(direct_engine, expire_on_commit=False,
                                   class_=AsyncSession)
```

Also patch both `app.database.AsyncSessionLocal` and `app.routers.samples.AsyncSessionLocal`
(the latter has its own reference from the `from app.database import AsyncSessionLocal`
import at module load time).

Also remove any `await db.refresh(obj)` calls that follow `await db.commit()`.
`expire_on_commit=False` + `db.flush()` already populates server-generated PKs
via RETURNING; the refresh is redundant and crosses a PgBouncer boundary.

### Applied to
`scripts/ingest_overnight.py` — `_install_direct_engine()` called at `run()` startup.

---

## 38. Railway Crash Loop: Loading CLAP at Startup via `on_event("startup")` Causes OOM

### Symptom
Railway deploy enters a restart loop immediately after `git push`.  Logs show:
```
INFO:     Started server process [1]
INFO:     Waiting for application startup.
[torch/huggingface warnings for CLAP weight download]
INFO:     Started server process [1]   ← process killed before startup completes
INFO:     Waiting for application startup.
```
The server never reaches `INFO: Application startup complete.`  `/health` returns
502 or times out indefinitely.

### Root Cause
A `@app.on_event("startup")` handler loaded CLAP (~900 MB) via
`await loop.run_in_executor(None, registry.clap)` before uvicorn finished startup.
Railway interprets the process as unhealthy if it does not begin serving within a
fixed timeout; the OOM kill (or slow load exceeding that timeout) triggers a restart.

### Fix
**Remove the startup warm-up entirely.**  CLAP loads lazily on the first
`POST /api/search/text` or `/api/search/audio` request.  Wrap those calls in
try/except and return `503 Service Unavailable` on any exception so a CLAP failure
does not crash the server process.

```python
# search.py — safe lazy-load pattern
def _clap_encode_text(text: str) -> list[float]:
    return registry.clap().encode_text(text)

@router.post("/text")
async def search_by_text(payload, db, current_user):
    loop = asyncio.get_running_loop()
    try:
        vector = await loop.run_in_executor(None, _clap_encode_text, payload.query)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Search unavailable: {exc}")
    ...
```

### Trade-off
The first search after a Railway restart is slow (~30 s) while CLAP loads.
This is acceptable for a demo; do **not** try to hide it with a warm-up task.

### Rule
Never load large ML models (>500 MB) in a FastAPI startup handler on Railway.
The server must start and pass its health check before doing any heavy work.

---

## 39. Vercel GitHub Integration: rootDirectory Must Be Set or It Tries to Install Python

### Symptom
GitHub-triggered Vercel builds fail immediately:
```
Using CPython 3.14.3
× No solution found when resolving dependencies:
╰─▶ Because torch==2.5.1+cpu has no wheels with a matching Python ABI tag (cp314)
```
Manual `vercel --prod` from `frontend/` succeeds; GitHub pushes always fail.

### Root Cause
The Vercel project's `rootDirectory` was `null`.  When Vercel clones the GitHub
repo at the repo root, it finds `requirements.txt` (the Python backend) and
runs `uv install`, which fails because PyTorch has no Python 3.14 wheels.

Manual CLI deploys from `frontend/` work because the CLI uploads only that
directory's files — the Python `requirements.txt` never enters the picture.

### Fix
Set `rootDirectory` to `frontend` (relative to the repo root) via the REST API:

```bash
VERCEL_TOKEN=$(cat ~/.local/share/com.vercel.cli/auth.json | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(list(d.values())[0])")

curl -X PATCH "https://api.vercel.com/v9/projects/<projectId>?teamId=<teamId>" \
  -H "Authorization: Bearer $VERCEL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"rootDirectory": "frontend", "framework": "vite"}'
```

After this, every GitHub push builds from `frontend/`, finds `package.json`,
and runs `vite build` correctly.

### Rule
Whenever a Vercel project is connected to a GitHub repo that contains both a
Python backend and a Node.js frontend in a subdirectory, always set
`rootDirectory` explicitly — never leave it `null`.

### CORS corollary
Each new Vercel deployment gets a unique preview URL (`-HASH-team.vercel.app`).
Add only the **stable aliases** to Railway's `ALLOWED_ORIGINS`:
- `https://audio-sample-manager.vercel.app`
- `https://audio-sample-manager-josh-miao-s-projects.vercel.app`

These aliases always point to the latest production deployment — no need to
update ALLOWED_ORIGINS on every redeploy.

---

## 40. `railway up` from WSL/OneDrive DrvFS: CRLF Files → Null-Byte SyntaxError

### Symptom
After `railway up` from a WSL path under `/mnt/c/…` (OneDrive), Railway crashes
on startup with:
```
SyntaxError: source code string cannot contain null bytes
File "/app/app/models/__init__.py", line 2, in <module>
    from .user import User
```
The local files are clean (no null bytes). `file app/models/user.py` shows
"ASCII text executable". The error reproduces on every `railway up` from that path
but goes away if you deploy from a native Linux path.

### Root Cause
OneDrive stores files with CRLF (`\r\n`) line endings on the Windows NTFS filesystem.
WSL DrvFS exposes those files as-is (CRLF). When `railway up` archives from the DrvFS
path, it bundles CRLF files. During the railpack/nixpacks Docker build, some layer
processing corrupts the CRLF bytes into null bytes — the exact mechanism is internal
to railpack but consistently reproducible.

### Fix (immediate)
Deploy from a clean git archive on native Linux instead of the DrvFS path:
```bash
mkdir -p /tmp/audio-sample-manager
git archive HEAD | tar -x -C /tmp/audio-sample-manager
cd /tmp/audio-sample-manager
railway up --project <id> --service <id> --environment <id> --ci
```
This produces LF-only files and eliminates the null-byte corruption.

### Fix (permanent)
Add `.gitattributes` to force LF for all text files in the repo. This normalises
line endings in git and in any downstream tool that respects git attributes:
```
* text=auto eol=lf
*.py text eol=lf
```
After this, `git checkout` on Windows will still produce CRLF on disk (DrvFS), but
tools that read the git object (like `git archive`) produce LF — safe for deployment.

---

## 41. asyncpg + PgBouncer Transaction Mode: NullPool Required on Railway

### Symptom
Railway FastAPI server crashes immediately after an OOM restart with:
```
DuplicatePreparedStatementError: prepared statement "__asyncpg_stmt_1__" already exists
InvalidSQLStatementNameError: prepared statement "__asyncpg_stmt_1a__" does not exist
```
All endpoints return 500. Setting `statement_cache_size=0` (the documented fix) does
not help.

### Root Cause
asyncpg uses named prepared statements per connection. PgBouncer transaction mode
re-uses server connections between asyncpg client connections. After an OOM crash,
asyncpg connections are abruptly dropped. PgBouncer's server connections retain the
stale prepared statements. The new asyncpg pool starts with `pool_size=10` and
initialises all 10 connections **concurrently**. Multiple asyncpg connections are
assigned the same PgBouncer server connection and both try to create
`__asyncpg_stmt_0__` → `DuplicatePreparedStatementError`.

`statement_cache_size=0` only disables caching; asyncpg still creates named prepared
statements for every execute call. With a shared server connection, the names collide.

### Fix
Switch `database.py` to `NullPool`. Each request opens its own asyncpg connection;
there is no concurrent pool initialisation and no shared server connection:
```python
from sqlalchemy.pool import NullPool
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    poolclass=NullPool,                     # replaces pool_size=10/max_overflow=20
    connect_args={"statement_cache_size": 0},
)
```

### Constraint
Railway free tier cannot reach Supabase direct PostgreSQL on port 5432 (ENETUNREACH).
Supabase session-mode pooler (same host, port 5432) is also unreachable from Railway.
Only port 6543 (transaction mode) works. NullPool + port 6543 is the viable option
for Railway.

---

## 42. asyncpg + PgBouncer: DuplicatePreparedStatementError Persists After Railway Restart

### Problem
Even with `NullPool` and `statement_cache_size=0`, Railway deployments get
`DuplicatePreparedStatementError` on every restart. The error appears on the
very first request after a redeploy:
```
prepared statement "__asyncpg_stmt_1__" already exists
[SQL: select pg_catalog.version()]
```

### Root Cause
asyncpg 0.29.0 uses a **module-level global counter** (`_uid`) for prepared statement
names — format `__asyncpg_{prefix}_{_uid:x}__`. This counter resets to 0 when the
Railway process restarts (new deployment).

PgBouncer's **server connections persist** across Railway process restarts — they're
maintained by Supabase's PgBouncer, not by the Railway container. After a restart:
1. The old process prepared `__asyncpg_stmt_1__` on server connection S1.
2. PgBouncer retained S1 (it's still alive on Supabase's side).
3. New Railway process starts, `_uid` resets to 0.
4. First request gets S1 via PgBouncer, tries to prepare `__asyncpg_stmt_1__`.
5. S1 already has that statement → `DuplicatePreparedStatementError`.

`NullPool` prevents the *concurrent pool-init* conflict (§41) but not the
*cross-restart* conflict.

### Fix
Monkey-patch asyncpg's `Connection._get_unique_id` in `app/database.py` before
engine creation to add a **per-process random base offset**:

```python
import uuid
import asyncpg.connection as _asyncpg_conn

_stmt_base = int(uuid.uuid4().hex[:12], 16)

def _unique_stmt_name(self, prefix):
    _asyncpg_conn._uid += 1
    return f"__{prefix}_{_stmt_base + _asyncpg_conn._uid:x}__"

_asyncpg_conn.Connection._get_unique_id = _unique_stmt_name
```

Each Railway restart generates a new random 48-bit base. The chance of the new
base colliding with any leftover statement name from a previous run is negligible.

### Why not DEALLOCATE ALL?
An alternative is to run `DEALLOCATE ALL` on each new asyncpg connection.
This is harder to wire in cleanly because SQLAlchemy's `connect` event fires
synchronously and the async asyncpg connection can't be awaited there.
The monkey-patch is simpler and equally correct.

### Long-term note
Prepared statements accumulate on PgBouncer server connections (they're never
DEALLOCATED because asyncpg with `statement_cache_size=0` simply doesn't cache
them — it doesn't explicitly deallocate either). For a demo project with few
restarts this is fine. For production, run `DEALLOCATE ALL` on connect or use
Supabase's session-mode pooler (port 5432, accessible from most non-Railway hosts).

---

## 43. CLAP OOM on Railway Free Tier Causes Silent Frontend Failure

### Symptom
Text search on the deployed frontend appeared to return "the same samples as the home page."
The UI showed no error; submitting a search query left the browse grid unchanged.

### Root Cause
`POST /api/search/text` loads LAION-CLAP (~900 MB weights) on first call via `registry.clap()`.
Railway's free tier hard-limits processes to 512 MB. The OS OOMs the process;
Railway returns 502 before FastAPI can emit its own 503.

The original `SearchBar.handleTextSearch` only had a `finally` block — no `catch`.
When Axios threw on the 502, `onResults()` was never called, so BrowsePage's
`searched` state stayed `false` and the samples grid kept showing the pre-search
home page list. The user read this as "search returned the home page samples."

### Fix
- Added `catch` in `SearchBar` for both text and audio search handlers.
- On 502/503, surface a specific message: "Semantic search needs CLAP, which can't
  run on the free-tier server (out of memory). Run the backend locally to use text
  search."
- Added `onError` prop to `SearchBar`; `BrowsePage` manages an `error` state and
  renders a dismissable red banner.

### How to actually use text/audio search
CLAP must be running in the same process that handles the search request.
Options in order of preference:
1. Run backend locally (`uvicorn app.main:app --reload`) → test at localhost:8000/docs.
2. Upgrade Railway to Starter plan ($5/mo, 8 GB RAM) → CLAP loads normally.
The 776 audio embeddings already in `audio_embeddings` were generated locally during
ingestion — the stored vectors are fine; only the *query* embedding path is broken
on Railway.

---

## 44. Vercel Build Cache Silently Ignores `.env.production` Changes

### Symptom
Updated `frontend/.env.production` with a new `VITE_SEARCH_URL` (ngrok URL).
Ran `vercel --prod`. The deployed site still sent search requests to Railway instead
of ngrok — the new URL was not in the bundle. ngrok showed 0 connections; Railway
continued to OOM on CLAP.

### Root Cause
Vercel's build cache is keyed on source file hashes. When only `.env.production`
changed (133 bytes), Vercel detected no changes in the JS/TS source files and
served the previous cached bundle. `VITE_*` variables are baked at Vite build time
— if the build is skipped, the old values remain.

### Fix
Always use `vercel --prod --force` when changing `frontend/.env.production`.
The `--force` flag bypasses the build cache ("Skipping build cache, deployment was
triggered without cache") and guarantees a fresh Vite build with the new env values.

### Rule
`vercel --prod` for code changes. `vercel --prod --force` for `.env.production` changes.

---

## 45. Vercel Preview URL vs Production URL — Different Bundles

### Symptom
After deploying with `vercel --prod --force`, tested text search on a Vercel URL
that looked like `https://audio-sample-manager-jjh3bu241-josh-miao-s-projects.vercel.app`.
Search still failed (ngrok showed 0 connections).

### Root Cause
Vercel creates a unique per-deployment preview URL for every deploy, including
production deploys. The preview URL (`-jjh3bu241-...`) points to that specific
deployment's bundle. But the `--force` rebuild produced a *new* deployment with a
different preview URL. The user was still on an old preview URL from a previous
(non-forced) build that had the stale bundle without `VITE_SEARCH_URL`.

### Fix
Always test the canonical production alias: `https://audio-sample-manager.vercel.app`.
That alias is updated atomically at deploy time to point to the latest production build.
Preview URLs are immutable snapshots — they never update.
