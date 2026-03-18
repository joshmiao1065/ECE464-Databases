# Debugging Lessons — Audio Sample Manager

Accumulated debugging knowledge for agents and developers working on this codebase.
Read this file at the start of every new conversation (it is listed in CLAUDE.md).
Update it immediately whenever a new bug is root-caused or a new pattern is discovered.

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
