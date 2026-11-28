"""In-memory one-time token store for the Teleport auth relay (/teleport-auth).

A login through a token renews db-gateway's own session (teleport_session).
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# Failed logins one link allows; each counts toward Teleport's own account lockout.
MAX_FAILED_LOGINS = 3

@dataclass
class AuthToken:
    token: str
    created_at: datetime
    ttl_minutes: int = 5
    used: bool = False
    failures: int = 0
    expires_at: datetime = field(init=False)

    def __post_init__(self) -> None:
        self.expires_at = self.created_at + timedelta(minutes=self.ttl_minutes)

    def is_valid(self) -> bool:
        return (not self.used) and datetime.now(timezone.utc) < self.expires_at


class TokenStore:
    """Thread-safe one-time-token store (5-minute TTL unless asked otherwise)."""

    def __init__(self) -> None:
        self._tokens: dict[str, AuthToken] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, ttl_minutes: int = 5) -> str:
        """Generate a fresh one-time token."""
        if not 1 <= ttl_minutes <= 120:
            raise ValueError("token ttl must be 1-120 minutes")
        tok = secrets.token_urlsafe(32)
        async with self._lock:
            self._cleanup()
            self._tokens[tok] = AuthToken(
                token=tok, created_at=datetime.now(timezone.utc), ttl_minutes=ttl_minutes,
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

    async def record_failure(self, tok: str) -> int:
        """Count a failed login through ``tok``; the link dies after MAX_FAILED_LOGINS."""
        async with self._lock:
            token = self._tokens.get(tok)
            if token is None:
                return MAX_FAILED_LOGINS
            token.failures += 1
            if token.failures >= MAX_FAILED_LOGINS:
                token.used = True
            return token.failures

    async def peek(self, tok: str) -> bool:
        """Check a token's validity without consuming."""
        return await self.lookup(tok) is not None

    async def lookup(self, tok: str) -> Optional[AuthToken]:
        """The valid, unused token, or None; does not consume."""
        async with self._lock:
            self._cleanup()
            token = self._tokens.get(tok)
            return token if token is not None and token.is_valid() else None

    def _cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [k for k, v in self._tokens.items() if v.expires_at < now]
        for key in expired:
            self._tokens.pop(key, None)
