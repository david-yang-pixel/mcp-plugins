from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Header, HTTPException, status

from .settings import get_settings


def require_api_key(authorization: Optional[str] = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_bearer_token"},
        )

    provided = authorization.removeprefix("Bearer ").strip()
    expected = get_settings().api_key_value
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "invalid_api_key"},
        )


def require_codex_completion_api_key(
    authorization: Optional[str] = Header(default=None),
) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_bearer_token"},
        )
    expected = get_settings().codex_completion_api_key_value
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "completion_callback_disabled"},
        )
    provided = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "invalid_api_key"},
        )
