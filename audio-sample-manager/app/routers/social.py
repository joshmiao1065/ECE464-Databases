"""
Social endpoints nested under /api/samples/{sample_id}/:

  GET    /{sample_id}/comments                  — list comments with usernames (public)
  POST   /{sample_id}/comments                  — post a comment (auth required)
  DELETE /{sample_id}/comments/{comment_id}     — delete own comment (auth required)

  GET    /{sample_id}/ratings/avg               — average score + vote count (public)
  POST   /{sample_id}/ratings                   — upsert rating 1-5 (auth required)

  GET    /{sample_id}/download                  — record download + redirect to file (optional auth)
  GET    /{sample_id}/downloads                 — total download count (public)
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user, get_optional_user
from app.models.sample import Sample
from app.models.social import Comment, Rating
from app.models.system import DownloadHistory
from app.models.user import User
from app.schemas.social import (
    CommentCreate, CommentOut,
    RatingCreate, RatingOut, RatingStats,
    DownloadStats,
)

router = APIRouter()


# ── Shared helper ─────────────────────────────────────────────────────────────

async def _require_sample(sample_id: uuid.UUID, db: AsyncSession) -> Sample:
    sample = await db.get(Sample, sample_id)
    if not sample:
        raise HTTPException(status_code=404, detail="Sample not found")
    return sample


# ── Comments ──────────────────────────────────────────────────────────────────

@router.get("/{sample_id}/comments", response_model=List[CommentOut])
async def list_comments(
    sample_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return all comments for a sample, oldest first, including commenter username."""
    rows = await db.execute(
        select(Comment, User.username)
        .outerjoin(User, Comment.user_id == User.id)
        .where(Comment.sample_id == sample_id)
        .order_by(Comment.created_at.asc())
    )
    return [
        CommentOut(
            id=c.id,
            user_id=c.user_id,
            username=username,
            sample_id=c.sample_id,
            text=c.text,
            created_at=c.created_at,
        )
        for c, username in rows.all()
    ]


@router.post("/{sample_id}/comments", response_model=CommentOut, status_code=201)
async def post_comment(
    sample_id: uuid.UUID,
    payload: CommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _require_sample(sample_id, db)
    comment = Comment(user_id=current_user.id, sample_id=sample_id, text=payload.text)
    db.add(comment)
    await db.commit()
    await db.refresh(comment)  # populate server-generated id and created_at
    return CommentOut(
        id=comment.id,
        user_id=comment.user_id,
        username=current_user.username,
        sample_id=comment.sample_id,
        text=comment.text,
        created_at=comment.created_at,
    )


@router.delete("/{sample_id}/comments/{comment_id}", status_code=204)
async def delete_comment(
    sample_id: uuid.UUID,
    comment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a comment.  Only the comment's author may delete it."""
    result = await db.execute(
        select(Comment).where(
            Comment.id == comment_id,
            Comment.sample_id == sample_id,
        )
    )
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Cannot delete another user's comment")
    await db.delete(comment)
    await db.commit()


# ── Ratings ───────────────────────────────────────────────────────────────────

@router.get("/{sample_id}/ratings/avg", response_model=RatingStats)
async def get_rating_stats(
    sample_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    row = await db.execute(
        select(func.avg(Rating.score), func.count(Rating.id))
        .where(Rating.sample_id == sample_id)
    )
    avg, count = row.one()
    return RatingStats(
        average=round(float(avg), 2) if avg is not None else None,
        count=count,
    )


@router.post("/{sample_id}/ratings", response_model=RatingOut)
async def upsert_rating(
    sample_id: uuid.UUID,
    payload: RatingCreate,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create or update the current user's rating for a sample (1–5 stars).
    Returns 201 on first submission, 200 on subsequent updates.
    """
    await _require_sample(sample_id, db)

    result = await db.execute(
        select(Rating).where(
            Rating.user_id == current_user.id,
            Rating.sample_id == sample_id,
        )
    )
    rating = result.scalar_one_or_none()
    if rating:
        rating.score = payload.score
        response.status_code = status.HTTP_200_OK
    else:
        rating = Rating(user_id=current_user.id, sample_id=sample_id, score=payload.score)
        db.add(rating)
        response.status_code = status.HTTP_201_CREATED

    await db.commit()
    await db.refresh(rating)
    return rating


# ── Download tracking ─────────────────────────────────────────────────────────

@router.get("/{sample_id}/download", response_class=RedirectResponse)
async def download_sample(
    sample_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """
    Record a download event and redirect to the audio file URL.
    Uses GET so browser links and <a href> tags work natively.
    Auth is optional — anonymous downloads are tracked with user_id=NULL.
    """
    sample = await _require_sample(sample_id, db)
    db.add(DownloadHistory(
        sample_id=sample_id,
        user_id=current_user.id if current_user else None,
    ))
    await db.commit()
    return RedirectResponse(url=sample.file_url, status_code=status.HTTP_302_FOUND)


@router.get("/{sample_id}/downloads", response_model=DownloadStats)
async def download_stats(
    sample_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
):
    """
    Return total download count for the sample, and optionally the current
    user's own download count if they are authenticated.
    """
    total_result = await db.execute(
        select(func.count(DownloadHistory.id))
        .where(DownloadHistory.sample_id == sample_id)
    )
    total = total_result.scalar_one()

    user_count: Optional[int] = None
    if current_user:
        user_result = await db.execute(
            select(func.count(DownloadHistory.id)).where(
                DownloadHistory.sample_id == sample_id,
                DownloadHistory.user_id == current_user.id,
            )
        )
        user_count = user_result.scalar_one()

    return DownloadStats(total=total, user_downloads=user_count)
