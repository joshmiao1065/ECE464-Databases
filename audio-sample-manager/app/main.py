from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from app.database import get_db
from app.models.system import ProcessingQueue
from app.routers import auth, samples, search, collections, social

app = FastAPI(
    title="Audio Sample Manager",
    description="MIR-powered sample discovery platform",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,        prefix="/api/auth",        tags=["auth"])
app.include_router(samples.router,     prefix="/api/samples",     tags=["samples"])
app.include_router(social.router,      prefix="/api/samples",     tags=["social"])
app.include_router(search.router,      prefix="/api/search",      tags=["search"])
app.include_router(collections.router, prefix="/api/collections",  tags=["collections"])


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok"}


@app.get("/api/admin/queue", tags=["meta"])
async def queue_status(db=Depends(get_db)):
    """Pipeline queue summary: counts per status + recent failures."""
    counts = {}
    for status in ("pending", "processing", "done", "failed"):
        n = (await db.execute(
            select(func.count()).where(ProcessingQueue.status == status)
        )).scalar_one()
        counts[status] = n

    failed_rows = (await db.execute(
        select(ProcessingQueue)
        .where(ProcessingQueue.status == "failed")
        .order_by(ProcessingQueue.updated_at.desc())
        .limit(20)
    )).scalars().all()

    return {
        "counts": counts,
        "total": sum(counts.values()),
        "percent_done": round(counts["done"] / max(sum(counts.values()), 1) * 100, 1),
        "recent_failures": [
            {
                "sample_id": str(r.sample_id),
                "retry_count": r.retry_count,
                "error": r.error_log,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in failed_rows
        ],
    }
