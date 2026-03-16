import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.deps import get_current_user, get_optional_user
from app.models.collection import Collection, CollectionItem
from app.models.sample import Sample
from app.models.user import User
from app.schemas.collection import CollectionCreate, CollectionOut
from app.schemas.sample import SampleOut

router = APIRouter()


# ── Helper ────────────────────────────────────────────────────────────────────

async def _own_collection(
    collection_id: uuid.UUID,
    db: AsyncSession,
    current_user: User,
) -> Collection:
    """Return the collection only if it belongs to current_user, else 404/403."""
    collection = await db.get(Collection, collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    if collection.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your collection")
    return collection


# ── Collection CRUD ───────────────────────────────────────────────────────────

@router.get("/", response_model=List[CollectionOut])
async def list_my_collections(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all collections owned by the authenticated user."""
    result = await db.execute(
        select(Collection)
        .where(Collection.user_id == current_user.id)
        .order_by(Collection.created_at.desc())
    )
    return result.scalars().all()


@router.post("/", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
async def create_collection(
    payload: CollectionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    collection = Collection(user_id=current_user.id, **payload.model_dump())
    db.add(collection)
    await db.commit()
    await db.refresh(collection)
    return collection


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    collection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    collection = await _own_collection(collection_id, db, current_user)
    await db.delete(collection)
    await db.commit()


# ── Collection items ──────────────────────────────────────────────────────────

@router.get("/{collection_id}/samples", response_model=List[SampleOut])
async def get_collection_samples(
    collection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """
    List samples in a collection.
    Private collections are only visible to their owner.
    Eagerly loads audio_metadata and tags so SampleOut is fully populated.
    """
    result = await db.execute(
        select(Collection)
        .options(
            selectinload(Collection.samples).options(
                selectinload(Sample.audio_metadata),
                selectinload(Sample.tags),
            )
        )
        .where(Collection.id == collection_id)
    )
    collection = result.scalar_one_or_none()
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    if collection.is_private:
        if not current_user or collection.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="This collection is private")
    return collection.samples


@router.post(
    "/{collection_id}/samples/{sample_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def add_to_collection(
    collection_id: uuid.UUID,
    sample_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _own_collection(collection_id, db, current_user)

    # Validate the sample exists before attempting the insert
    if not await db.get(Sample, sample_id):
        raise HTTPException(status_code=404, detail="Sample not found")

    # Idempotent: if the item is already in the collection, do nothing
    existing = await db.execute(
        select(CollectionItem).where(
            CollectionItem.collection_id == collection_id,
            CollectionItem.sample_id == sample_id,
        )
    )
    if existing.scalar_one_or_none():
        return  # already present — 204 with no further action

    db.add(CollectionItem(collection_id=collection_id, sample_id=sample_id))
    await db.commit()


@router.delete(
    "/{collection_id}/samples/{sample_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_from_collection(
    collection_id: uuid.UUID,
    sample_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _own_collection(collection_id, db, current_user)
    result = await db.execute(
        select(CollectionItem).where(
            CollectionItem.collection_id == collection_id,
            CollectionItem.sample_id == sample_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not in collection")
    await db.delete(item)
    await db.commit()
