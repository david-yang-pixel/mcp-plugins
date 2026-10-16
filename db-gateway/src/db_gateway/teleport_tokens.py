"""In-memory one-time token store for Teleport auth relay."""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class AuthToken:
    token: str
    created_at: datetime
    used: bool = False
    expires_at: datetime = field(init=False)

    def __post_init__(self) -> None:
        self.expires_at = self.created_at + timedelta(minutes=5)

    def is_valid(self) -> bool:
        return (not self.used) and datetime.now(timezone.utc) < self.expires_at


class TokenStore:
    """Thread-safe one-time-token store with 5-min TTL."""

    def __init__(self) -> None:
        self._tokens: dict[str, AuthToken] = {}
        self._lock = asyncio.Lock()

    async def create(self) -> str:
        """Generate a fresh one-time token."""
        tok = secrets.token_urlsafe(32)
        async with self._lock:
            self._cleanup()
            self._tokens[tok] = AuthToken(
                token=tok, created_at=datetime.now(timezone.utc),
            )
        return tok

    async def consume(self, tok: str) -> bool:
        """Mark a token as used. Returns True if valid and newly consumed."""
        async with self._lock:
            self._cleanup()
            token = self._tokens.get(tok)
            if token is None or not token.is_valid():
                return False
            token.used = True
            return True

    async def peek(self, tok: str) -> bool:
        """Check a token's validity without consuming."""
        async with self._lock:
            self._cleanup()
            token = self._tokens.get(tok)
            return token is not None and token.is_valid()

    def _cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [k for k, v in self._tokens.items() if v.expires_at < now]
        for key in expired:
            self._tokens.pop(key, None)
