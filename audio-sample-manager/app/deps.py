from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User

_oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/token")
_oauth2_optional = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)

def _credentials_exc() -> HTTPException:
    """
    Return a fresh 401 HTTPException each time it is called.
    Reusing a singleton exception instance causes Python to re-attach the
    previous traceback/__context__ on every raise, which pollutes error logs.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decode_user_id(token: str) -> Optional[str]:
    """Decode a JWT and return the 'sub' claim, or None on any error."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


async def get_current_user(
    token: str = Depends(_oauth2),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency: resolve Bearer token → active User, or raise 401."""
    user_id = _decode_user_id(token)
    if not user_id:
        raise _credentials_exc()

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise _credentials_exc()
    return user


async def get_optional_user(
    token: Optional[str] = Depends(_oauth2_optional),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """
    Like get_current_user but returns None instead of raising 401 when no
    token is present.  Use on endpoints that work for both guests and members.
    """
    if not token:
        return None
    user_id = _decode_user_id(token)
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()
