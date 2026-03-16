from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
