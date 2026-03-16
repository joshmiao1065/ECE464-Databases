from typing import AsyncIterator

import httpx

from app.config import settings

FREESOUND_BASE = "https://freesound.org/apiv2"

# Fields requested on every sound fetch — keep minimal to stay under rate limits
_SOUND_FIELDS = "id,name,description,duration,previews,pack,tags,username,filesize,samplerate"


class FreesoundClient:
    """
    Async client for the Freesound APIv2.

    Auth: Token-based (client credentials flow — no user redirect required).
    Rate limit: ~2,000 requests/day on the free tier. Cache everything you pull.
    """

    def __init__(self):
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Token {settings.FREESOUND_API_KEY}"},
            timeout=30.0,
        )

    async def search_sounds(
        self, query: str, page: int = 1, page_size: int = 150
    ) -> dict:
        resp = await self._client.get(
            f"{FREESOUND_BASE}/search/text/",
            params={
                "query": query,
                "page": page,
                "page_size": page_size,
                "fields": _SOUND_FIELDS,
                "format": "json",
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def get_sound(self, sound_id: int) -> dict:
        resp = await self._client.get(
            f"{FREESOUND_BASE}/sounds/{sound_id}/",
            params={"fields": _SOUND_FIELDS},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_pack(self, pack_id: int) -> dict:
        resp = await self._client.get(f"{FREESOUND_BASE}/packs/{pack_id}/")
        resp.raise_for_status()
        return resp.json()

    async def download_preview(self, preview_url: str) -> bytes:
        """Download an MP3 preview (HQ, ~128 kbps). Pipe straight to Supabase Storage."""
        resp = await self._client.get(preview_url)
        resp.raise_for_status()
        return resp.content

    async def iter_all_sounds(self, query: str) -> AsyncIterator[dict]:
        """Paginate through all results for a query, yielding one sound dict at a time."""
        page = 1
        while True:
            data = await self.search_sounds(query, page=page)
            for sound in data.get("results", []):
                yield sound
            if not data.get("next"):
                break
            page += 1

    async def close(self):
        await self._client.aclose()

    # Support async context manager usage
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
