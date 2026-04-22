"""Shared FastAPI dependencies."""
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from shared.config import settings
from shared.db.postgres import get_pool as _get_pool
from shared.redis.client import get_redis as _get_redis

_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    """Validate Bearer token. Skipped if ADMIN_API_TOKEN is not configured."""
    token = settings.admin_api_token
    if not token:
        return  # auth disabled — no token configured
    if credentials is None or credentials.credentials != token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_pool():
    return await _get_pool()


async def get_redis():
    return await _get_redis()
