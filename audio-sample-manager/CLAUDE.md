> **Agents:** Also read `../LESSONS.md` (one level up, in the repo root) at the
> start of every conversation. It contains hard-won debugging patterns for
> SQLAlchemy async, TF eager execution, MusiCNN subprocess isolation, asyncio
> event-loop lifetime, and more. Update it immediately whenever a new bug is
> root-caused. Keeping LESSONS.md current makes every future debugging session
> shorter.

# Audio Sample Manager — Project Context

MIR-powered audio sample discovery platform built as a Cooper Union Databases final project.
Producers search a library of audio samples using natural language or by uploading a reference clip.
The backend automatically classifies every sample with BPM, key, energy, and semantic tags.

---

## Tech Stack

| Layer | Technology | Notes |
|---|---|---|
| Web framework | FastAPI 0.111 + Uvicorn | Fully async; all handlers are `async def` |
| ORM | SQLAlchemy 2.0 (async) | `AsyncSession`, `async_sessionmaker`, mapped columns |
| Database | PostgreSQL + pgvector extension | `asyncpg` driver; HNSW index for cosine search |
| Migrations | Alembic 1.13 | Async migration runner in `alembic/env.py` |
| File storage | Supabase Storage (S3-compatible) | Audio previews stored as `freesound/<id>.mp3` |
| Auth | JWT via `python-jose` + `passlib[bcrypt]` | Bearer tokens; `app/deps.py` has the two dependencies |
| Audio features | Librosa 0.10 | BPM, key, RMS energy, loudness, spectral centroid, ZCR |
| Embeddings | LAION-CLAP (`laion-clap` 1.1.4) | 512-dim audio/text joint embedding; ~900 MB weights |
| Sound events | Google YAMNet (TF Hub) | 521 AudioSet classes; fine-grained event labels |
| Music tags | MTG MusiCNN (`musicnn` 0.1.6) | MagnaTagATune labels; genre/mood/instrumentation |
| Scraper | Freesound APIv2 (`httpx`) | Token-based auth; downloads HQ MP3 previews |
| Schema validation | Pydantic v2 | `model_config = {"from_attributes": True}` on all ORM-backed schemas |

---

## Directory Structure

```
audio-sample-manager/
├── alembic/
│   ├── env.py                    # Async migration runner; reads DATABASE_URL from settings
│   └── versions/
│       └── 001_initial_schema.py # All 17 tables + HNSW index + enums + extensions
├── alembic.ini                   # URL placeholder only; real URL set in env.py
├── app/
│   ├── main.py                   # FastAPI app, CORS, router registration
│   ├── config.py                 # Pydantic-settings; loads .env
│   ├── database.py               # Async engine + AsyncSessionLocal + get_db()
│   ├── deps.py                   # get_current_user / get_optional_user (JWT → User)
│   ├── models/
│   │   ├── base.py               # DeclarativeBase
│   │   ├── __init__.py           # Re-exports all models so Alembic discovers them
│   │   ├── user.py               # users table
│   │   ├── sample.py             # samples table (core entity)
│   │   ├── audio_embedding.py    # audio_embeddings (512-dim vector, 1:1 with samples)
│   │   ├── audio_metadata.py     # audio_metadata (Librosa features, 1:1 with samples)
│   │   ├── tag.py                # tags + sample_tags (M:M)
│   │   ├── pack.py               # packs + pack_samples
│   │   ├── collection.py         # collections + collection_items
│   │   ├── social.py             # comments + ratings
│   │   └── system.py             # download_history, search_queries, processing_queue, api_audit_log
│   ├── routers/
│   │   ├── auth.py               # POST /api/auth/register, /api/auth/token
│   │   ├── samples.py            # GET/POST /api/samples + _run_mir_pipeline background task
│   │   ├── search.py             # POST /api/search/text, /api/search/audio
│   │   ├── social.py             # Comments, ratings, download tracking (all under /api/samples/{id}/)
│   │   └── collections.py        # CRUD for collections + item management
│   ├── schemas/
│   │   ├── sample.py             # SampleOut, SampleCreate, AudioMetadataOut, TagOut
│   │   ├── search.py             # TextSearchRequest, SearchResponse
│   │   ├── social.py             # CommentOut/Create, RatingOut/Create, RatingStats, DownloadStats
│   │   ├── collection.py         # CollectionOut, CollectionCreate
│   │   └── user.py               # UserCreate, UserOut, Token
│   ├── workers/
│   │   ├── registry.py           # lru_cache singletons: clap(), yamnet(), musicnn()
│   │   ├── clap_worker.py        # CLAPWorker: encode_text(), encode_audio()
│   │   ├── librosa_worker.py     # extract_features() → dict matching audio_metadata columns
│   │   ├── yamnet_worker.py      # YAMNetWorker: predict() → List[str] sound event labels
│   │   └── musicnn_worker.py     # MusiCNNWorker: predict() → List[str] music tags
│   └── scraper/
│       └── freesound.py          # FreesoundClient: search_sounds, get_sound, iter_all_sounds, download_preview
├── scripts/
│   ├── ingest_freesound.py       # CLI: ingest Freesound samples; optional --process flag runs MIR pipeline
│   └── process_queue.py          # CLI: batch MIR worker; polls processing_queue for pending rows
└── frontend/                     # React + Vite + Wavesurfer.js (see Frontend section below)
```

---

## Environment Setup

Copy `.env.example` to `.env` and fill in all values:

```env
# asyncpg connection string (required)
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/audio_samples

# Supabase (required — used for file storage)
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_ANON_KEY=your-anon-key-here
SUPABASE_SERVICE_KEY=your-service-role-key-here
SUPABASE_STORAGE_BUCKET=audio-previews

# Freesound API (only API_KEY is required; CLIENT_ID/SECRET are optional OAuth fields)
FREESOUND_API_KEY=your-freesound-api-key
FREESOUND_CLIENT_ID=                  # optional
FREESOUND_CLIENT_SECRET=              # optional

# JWT (required)
SECRET_KEY=generate-a-strong-secret-key-here
ACCESS_TOKEN_EXPIRE_MINUTES=30
```

Install dependencies:
```bash
pip install -r requirements.txt
```

---

## Database: Schema

PostgreSQL with the `vector` and `uuid-ossp` extensions. All PKs are `UUID` generated by `uuid_generate_v4()`. All timestamps are `TIMESTAMPTZ`.

### Core entities

**`users`**
- `id`, `email` (unique), `username` (unique), `hashed_password` (bcrypt)
- `preferences_json` (JSON, nullable), `is_active` (bool), `created_at`, `updated_at`

**`samples`** — the central table
- `id`, `title`, `freesound_id` (int, nullable, unique), `file_url` (Supabase Storage URL)
- `waveform_url` (nullable), `duration_ms`, `file_size_bytes`, `mime_type`
- `user_id_owner` → `users.id` (SET NULL), `pack_id` → `packs.id` (SET NULL)
- `created_at`

**`audio_embeddings`** — 1:1 with samples
- `sample_id` (FK → samples, CASCADE, UNIQUE)
- `embedding` — `vector(512)` CLAP output
- `model_version` (default `'clap-htsat-fused'`)
- **HNSW index** on `embedding` using `vector_cosine_ops` (m=16, ef_construction=64)

**`audio_metadata`** — 1:1 with samples, populated by Librosa
- `sample_id` (FK → samples, CASCADE, UNIQUE)
- `bpm` (float), `key` (varchar 4, e.g. `"C#"`), `energy_level`, `loudness_lufs`
- `spectral_centroid`, `zero_crossing_rate`, `sample_rate` (int)
- `is_processed` (bool, default false) — flip to true when pipeline completes

### Categorisation

**`tags`** — flat taxonomy
- `name` (varchar 100, unique, indexed), `category` (varchar 50, indexed)
- `category` values: `"yamnet"` (sound event labels), `"musicnn"` (music tags), `"manual"` (user-applied)

**`sample_tags`** — M:M junction
- PK: `(sample_id, tag_id)`
- `source` (varchar 20): `"auto"` for pipeline-generated, `"manual"` for user-applied

### Collections / packs

**`packs`** — Freesound packs or user-curated groups
- `freesound_pack_id` (int, nullable, unique) — NULL for user-created packs

**`pack_samples`** — M:M junction for curated packs (`pack_id`, `sample_id`)

**`collections`** — user-owned playlists
- `user_id` → `users.id` (CASCADE), `name`, `description`, `is_private` (bool)

**`collection_items`** — M:M junction (`collection_id`, `sample_id`, `added_at`)

### Social

**`comments`** — `user_id` (SET NULL on delete), `sample_id` (CASCADE), `text`, `created_at`

**`ratings`** — one per (user, sample)
- `score` SmallInt with CHECK (1–5)
- UNIQUE constraint on `(user_id, sample_id)`

### System

**`download_history`** — `user_id` (nullable, SET NULL), `sample_id` (CASCADE), `downloaded_at`

**`search_queries`** — analytics log; `query_type` enum (`text` | `audio`), `result_count`, optional `user_id`

**`processing_queue`** — drives MIR pipeline
- `status` enum: `pending` → `processing` → `done` | `failed`
- `retry_count`, `worker_id` (for stall detection), `error_log` (on failure)
- `created_at`, `updated_at` (note: `updated_at` has no auto-update trigger — set manually in code)

**`api_audit_log`** — `endpoint`, `method`, `status_code`, `user_id`, `duration_ms`

---

## Database: Migration Workflow

The migration runner is async. Alembic's `env.py` reads `DATABASE_URL` from `app.config.settings` — the placeholder in `alembic.ini` is ignored.

```bash
# Apply all pending migrations (run this before first start)
alembic upgrade head

# Roll back the last migration
alembic downgrade -1

# Roll back everything
alembic downgrade base

# Generate a new auto-migration after changing an ORM model
alembic revision --autogenerate -m "describe the change"

# Check current revision
alembic current
```

**When adding a new table or column:**
1. Edit the ORM model in `app/models/`
2. Add the model import to `app/models/__init__.py` (Alembic discovers models via `Base.metadata`)
3. Run `alembic revision --autogenerate -m "your message"`
4. Review the generated file in `alembic/versions/`
5. Run `alembic upgrade head`

**Important:** pgvector's `vector(512)` type and the HNSW index are created with raw `op.execute()` calls — autogenerate does not handle these. If you ever recreate the `audio_embeddings` table, restore those manually from `001_initial_schema.py`.

---

## MIR Pipeline

When a sample is created via `POST /api/samples/`, a `ProcessingQueue` row is inserted and `_run_mir_pipeline(sample_id)` is registered as a FastAPI `BackgroundTask`. The pipeline runs after the HTTP response is sent.

### Pipeline steps (`app/routers/samples.py: _run_mir_pipeline`)

The pipeline is split across **three separate DB sessions** to prevent connection
timeouts. Supabase PgBouncer recycles idle connections after ~30 s; holding one
session open across 60–120 s of ML inference caused `ConnectionResetError` followed
by `PendingRollbackError`. See LESSONS.md §1 for full details.

```
Session A (< 1 s):
  1. Atomically claim ProcessingQueue entry (UPDATE WHERE status='pending')
  2. Fetch sample.file_url
  3. Close session — connection returned to pool.

No session (60–120 s):
  4. Download audio bytes from Supabase Storage via httpx
  5. [thread] Librosa  → extract_features(audio_bytes)
  6. [thread] CLAP     → encode_audio(audio_bytes)
  7. [thread] YAMNet   )
     [thread] MusiCNN  ) → asyncio.gather (concurrent)

Session B (< 1 s):
  8. DELETE + INSERT audio_metadata (Librosa features)
  9. DELETE + INSERT audio_embeddings (CLAP 512-dim vector)
 10. UPSERT tags + sample_tags (YAMNet + MusiCNN labels)
 11. Mark ProcessingQueue.status = 'done'
 12. Close session.

On exception: Session C (fresh) marks status = 'failed' + writes error_log.
```

Steps 5–7 use `loop.run_in_executor(None, ...)` to avoid blocking the async event
loop. YAMNet and MusiCNN run concurrently via `asyncio.gather`.

### Worker registry (`app/workers/registry.py`)

All three ML workers are lazy singletons via `@functools.lru_cache`. Weights load exactly once per process:

```python
from app.workers import registry

registry.clap()    # → CLAPWorker   (LAION-CLAP, ~900 MB, loads on first call)
registry.yamnet()  # → YAMNetWorker (TF Hub model, downloads on first call)
registry.musicnn() # → MusiCNNWorker (MTT_musicnn checkpoint)
```

**Never instantiate workers directly.** Always go through the registry so there is only one copy of the weights in memory.

**Critical — do NOT `import musicnn.tagger` in the main process.** That module calls
`tf.compat.v1.disable_eager_execution()` at import time, which silently breaks
YAMNet. The registry's `musicnn()` function is safe because `MusiCNNWorker` only
imports musicnn inside a subprocess. See LESSONS.md §2 for full details.

### Worker details

| Worker | Input | Output | Notes |
|---|---|---|---|
| `CLAPWorker.encode_audio(bytes)` | Raw audio bytes (any format) | `list[float]` len 512 | Resamples to 48 kHz mono via librosa; writes temp WAV, encodes, deletes temp file |
| `CLAPWorker.encode_text(str)` | Natural language string | `list[float]` len 512 | Used by search endpoint |
| `extract_features(bytes)` | Raw audio bytes | `dict` matching `audio_metadata` columns | Returns `bpm, key, energy_level, loudness_lufs, spectral_centroid, zero_crossing_rate, sample_rate` |
| `YAMNetWorker.predict(bytes, top_k=5)` | Raw audio bytes | `List[str]` | 521 AudioSet classes; resamples to 16 kHz |
| `MusiCNNWorker.predict(bytes, top_k=5)` | Raw audio bytes | `List[str]` | MagnaTagATune ~50 classes; **runs in subprocess** — see `musicnn_worker.py` |

**MusiCNN subprocess isolation** (`app/workers/musicnn_worker.py`):
MusiCNNWorker runs musicnn inside a persistent `ProcessPoolExecutor(spawn=1)`.
This prevents `tf.compat.v1.disable_eager_execution()` from contaminating the
main process. The subprocess is recreated automatically if it crashes
(`BrokenProcessPool` handler with single retry). Each call writes a temp MP3,
submits to the subprocess, and deletes the temp file in a `finally` block.
Audio shorter than 3 s returns `[]` instead of crashing (musicnn `UnboundLocalError`
on `batch` variable). See LESSONS.md §2 and §3.

### Tag deduplication

`_upsert_tag(db, sample_id, tag_name, category, seen_tag_ids)` handles the case where YAMNet and MusiCNN produce the same label. A `set` of already-written `tag.id` values is passed through both loops; the second occurrence is silently skipped, preventing a PK violation on `sample_tags`.

---

## Authentication

JWT Bearer token scheme. Tokens are HS256-signed with `SECRET_KEY`.

**Flow:**
1. `POST /api/auth/register` — creates user, returns `UserOut` (no token)
2. `POST /api/auth/token` — OAuth2 password form; returns `{"access_token": "...", "token_type": "bearer"}`
3. Include `Authorization: Bearer <token>` header on protected endpoints

**Dependencies in `app/deps.py`:**
- `get_current_user` — required auth; raises 401 on missing/invalid/expired token or inactive user
- `get_optional_user` — optional auth; returns `None` for unauthenticated callers (used on mixed-access endpoints like search, download)

Both dependencies are factory functions that return a fresh `HTTPException` on each call (not a singleton) to keep tracebacks clean.

---

## API Endpoints

All routes are prefixed with `/api`. The Swagger UI is available at `http://localhost:8000/docs`.

### Auth — `/api/auth`
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/register` | — | Create account; returns UserOut |
| POST | `/token` | — | Login; returns JWT access token |

### Samples — `/api/samples`
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | — | List samples (limit/offset); eager-loads metadata + tags |
| GET | `/{id}` | — | Single sample with metadata + tags; 422 on invalid UUID |
| POST | `/` | — | Create sample + queue MIR pipeline; returns SampleOut |

### Social — `/api/samples/{id}/...`
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/{id}/comments` | — | List comments oldest-first; includes `username` |
| POST | `/{id}/comments` | required | Post comment; returns CommentOut with username |
| DELETE | `/{id}/comments/{comment_id}` | required | Delete own comment; 403 on others' comments |
| GET | `/{id}/ratings/avg` | — | `{average, count}`; average is null if no ratings yet |
| POST | `/{id}/ratings` | required | Upsert rating 1–5; 201 on create, 200 on update |
| GET | `/{id}/download` | optional | Record DownloadHistory + 302 redirect to file URL |
| GET | `/{id}/downloads` | optional | `{total, user_downloads}`; user_downloads null if not authed |

### Search — `/api/search`
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/text` | optional | Text → CLAP embedding → pgvector cosine search |
| POST | `/audio` | optional | Upload audio → CLAP embedding → pgvector cosine search |

CLAP inference runs in a thread executor on both routes to avoid blocking the event loop. Both log to `search_queries` with optional `user_id`.

**Vector search pattern** (`search.py: _vector_search`):
1. Raw SQL with pgvector `<=>` operator to get ordered UUIDs (preserves distance ranking)
2. Second ORM query with `selectinload(Sample.audio_metadata, Sample.tags)` on those UUIDs
3. Re-sort by original distance order using a dict lookup

### Collections — `/api/collections`
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | required | List current user's collections |
| POST | `/` | required | Create collection |
| DELETE | `/{id}` | required | Delete own collection (cascades items) |
| GET | `/{id}/samples` | optional | List samples; 403 if private and not owner |
| POST | `/{id}/samples/{sample_id}` | required | Add sample (idempotent; validates sample exists first) |
| DELETE | `/{id}/samples/{sample_id}` | required | Remove sample from collection |

---

## Freesound Scraper

`app/scraper/freesound.py` — async client for Freesound APIv2. Token-based auth using `FREESOUND_API_KEY`. Rate limit ~2,000 requests/day (search calls only; preview downloads don't count).

```python
async with FreesoundClient() as client:
    async for sound in client.iter_all_sounds("kick drum"):
        audio_bytes = await client.download_preview(sound["previews"]["preview-hq-mp3"])
```

Fields requested on every sound: `id, name, description, duration, previews, pack, tags, username, filesize, samplerate`

### Ingestion script

```bash
# Ingest samples (queue MIR pipeline for later processing)
python -m scripts.ingest_freesound "kick drum" --limit 200

# Ingest and immediately run full MIR pipeline on each sample (slow for large batches)
python -m scripts.ingest_freesound "ambient pad" --limit 50 --process
```

The script:
1. Skips sounds already in the DB (checks `freesound_id`)
2. Downloads HQ MP3 preview
3. Uploads to Supabase Storage at `freesound/<id>.mp3`
4. Inserts `Sample` + `ProcessingQueue(pending)` row
5. If `--process`: calls `_run_mir_pipeline(sample.id)` inline

---

## Running the Server

```bash
# Development
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Production
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Use `--workers 1` in production — the ML worker singletons (CLAP, YAMNet, MusiCNN) are not safe to share across forked processes. If you need concurrency, use an async-native approach (multiple uvicorn instances behind a load balancer, each with their own model copies).

CORS is configured for `http://localhost:5173` (Vite dev server). Add production origins in `app/main.py`.

---

## Key Design Decisions

**Why two queries in vector search?**
pgvector's `<=>` operator returns raw row mappings, not ORM objects. A second ORM query with `selectinload` is needed to populate `audio_metadata` and `tags` on `SampleOut`. The first query uses a raw SQL to preserve distance order; the second fetches full ORM objects by ID; results are re-sorted with a dict lookup.

**Why `expire_on_commit=False` on `AsyncSessionLocal`?**
In async SQLAlchemy, accessing expired attributes after a commit would require an implicit I/O operation, which is not allowed in async context. `expire_on_commit=False` keeps attribute values accessible after `await db.commit()` without needing `await db.refresh()`.

**Why not store CLAP embeddings for search queries?**
`search_queries` only stores `query_text` and `result_count`, not the query vector. At scale, storing a 512-dim float array per search query is expensive. The query vector can be recomputed on demand.

**Why `processing_queue` instead of a job queue like Celery?**
Simplicity — FastAPI `BackgroundTasks` is sufficient for a demo/class project. The `processing_queue` table tracks status so failures are visible. In production you'd replace `BackgroundTasks` with a proper queue (Celery + Redis, ARQ, etc.) and have workers poll `processing_queue` for pending jobs.

**Why are tag categories `"yamnet"` vs `"musicnn"` instead of `"auto"`?**
Tags are shared across samples. If a tag named `"guitar"` was first created by YAMNet for one sample and then by MusiCNN for another, the category would conflict if both used `"auto"`. Storing the source model as the category lets you filter by which ML system assigned the tag.

---

## Frontend (React + Vite + Wavesurfer.js)

Source lives in `frontend/`. Start the dev server:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

The Vite dev server proxies all `/api/*` requests to `http://localhost:8000` — no CORS setup needed during development. In production, set `VITE_API_URL` in `frontend/.env`.

### Frontend directory structure

```
frontend/src/
├── api/
│   ├── client.ts         # Axios instance; JWT interceptor; 401 → logout
│   ├── samples.ts        # list, get, text search, audio search, comments, ratings, downloads
│   ├── auth.ts           # login (form-encoded), register
│   └── collections.ts    # list, create, delete, get samples, add/remove item
├── components/
│   ├── Navbar.tsx        # Sticky nav; login/logout/collections links
│   ├── SearchBar.tsx     # Text search form + audio file upload (two-mode toggle)
│   ├── SampleCard.tsx    # Grid card: title, BPM/key/duration chips, tag pills, link to detail
│   └── WavePlayer.tsx    # Wavesurfer.js waveform + play/pause + timestamp
├── hooks/
│   └── useWaveSurfer.ts  # Creates/destroys WaveSurfer instance on URL change
├── pages/
│   ├── BrowsePage.tsx    # / — browse list with search bar + pagination
│   ├── SamplePage.tsx    # /samples/:id — detail: waveform, metadata, tags, rating, comments, collections
│   ├── LoginPage.tsx     # /login
│   ├── RegisterPage.tsx  # /register
│   └── CollectionsPage.tsx  # /collections — list, create, delete, expand to see samples
├── store/
│   └── authStore.ts      # Zustand store: token/username in localStorage; login/register/logout
└── types/
    └── index.ts          # TypeScript types mirroring backend Pydantic schemas
```

### Auth flow in the frontend

1. `useAuthStore` reads `access_token` and `username` from localStorage on init.
2. `api/client.ts` attaches `Authorization: Bearer <token>` via Axios interceptor.
3. 401 responses clear the token (interceptor in `client.ts`), effectively logging out.
4. `Navbar` shows user-specific links based on `username` from the store.

---

## Bulk Pipeline Worker

`scripts/process_queue.py` — polls `processing_queue` for `status='pending'` rows and runs `_run_mir_pipeline` on each. Use this for samples ingested without the `--process` flag.

```bash
# Process current backlog then exit
python -m scripts.process_queue --once

# Run continuously (poll every 10 s)
python -m scripts.process_queue

# Custom poll interval and retry limit
python -m scripts.process_queue --poll-interval 5 --max-retries 2 --stale-minutes 10

# Reset all 'failed' entries back to 'pending' (retry them)
python -m scripts.process_queue --reset-failed

# Re-process 'done' samples that have no YAMNet/MusiCNN tags
python -m scripts.process_queue --requeue-done-missing-tags

# Combine: reset failed + requeue untagged, then process everything
python -m scripts.process_queue --reset-failed --requeue-done-missing-tags --once
```

### How it works

- Claims one `pending` entry at a time using `SELECT … FOR UPDATE SKIP LOCKED` (safe for concurrent workers)
- Sets `worker_id` to `hostname-pid` for stall detection
- Resets `processing` entries that haven't been updated in `--stale-minutes` back to `pending`
- Increments `retry_count` on failure; skips samples that reach `--max-retries` (marks as `failed`)
- Handles `SIGTERM` / `SIGINT` gracefully — finishes the current job then exits
- All async work runs in a **single `asyncio.run(_main())`** call so asyncpg's
  connection pool is never bound to a stale event loop (see LESSONS.md §4)

### Monitoring queue progress

```bash
curl http://localhost:8000/api/admin/queue
```

Returns JSON:
```json
{
  "counts": {"pending": 1100, "processing": 1, "done": 90, "failed": 2},
  "total": 1193,
  "percent_done": 7.5,
  "recent_failures": [{"sample_id": "...", "retry_count": 1, "error": "..."}]
}
```

This endpoint is defined in `app/main.py` as `GET /api/admin/queue`.

---

## API Endpoints (additional)

### Admin / Meta
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | — | Returns `{"status": "ok"}` |
| GET | `/api/admin/queue` | — | Pipeline queue summary: counts per status, percent done, recent failures |

---

## Known Limitations

- **`processing_queue.updated_at`** has no PostgreSQL trigger — it always shows the insert time. Set it manually in code on status changes, or add a trigger via migration.
- **No rate limiting** — consider `slowapi` for production.
- **MusiCNN `predict` uses the MTT_musicnn model** (MagnaTagATune, ~50 tags). The MSD_musicnn model (Million Song Dataset, more tags) is also available — swap `model="MTT_musicnn"` in `musicnn_worker.py` if you want broader coverage.
- **No audio upload to storage** — `POST /api/samples/` takes a `file_url` that must already be in Supabase Storage. There's no endpoint to upload the audio file itself. The ingestion script handles this for Freesound content.
- **Librosa key detection** only returns the root note (C, C#, D…) — mode (major/minor) detection is a future enhancement noted in the code.
- **CLAP hangs on very short audio** (< ~0.1 s at 48 kHz). Samples stuck in
  `processing` for > 5 min are likely very short clips. See LESSONS.md §8.
  The stale-detection mechanism in `process_queue.py` will eventually reset these.
- **MusiCNN returns `[]` for audio < 3 s** (musicnn's analysis window). This is
  by design — the UnboundLocalError is caught and treated as "no tags".
