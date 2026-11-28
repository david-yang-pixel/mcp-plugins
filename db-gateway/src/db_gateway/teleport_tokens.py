"""In-memory one-time token store for Teleport auth relay.

Each token names the session a login through it renews:

* ``SCOPE_SESSION`` - db-gateway's own session (teleport_session), the links
  db-gateway sends when automatic login stopped;
* ``SCOPE_HOST`` - the host session at ``teleport.auth_home``, the links the
  legacy tsh-auth-watchdog mints through ``/internal/teleport-auth/issue``
  (the host tunnels still depend on it until step 2 moves them).
"""
from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

SCOPE_SESSION = "session"
SCOPE_HOST = "host"

@dataclass
class AuthToken:
    token: str
    created_at: datetime
    scope: str = SCOPE_SESSION
    ttl_minutes: int = 5
    used: bool = False
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

    async def create(self, scope: str = SCOPE_SESSION, *, ttl_minutes: int = 5) -> str:
        """Generate a fresh one-time token for ``scope``."""
        if scope not in (SCOPE_SESSION, SCOPE_HOST):
            raise ValueError(f"unknown token scope: {scope}")
        if not 1 <= ttl_minutes <= 120:
            raise ValueError("token ttl must be 1-120 minutes")
        tok = secrets.token_urlsafe(32)
        async with self._lock:
            self._cleanup()
            self._tokens[tok] = AuthToken(
                token=tok, created_at=datetime.now(timezone.utc), scope=scope,
                ttl_minutes=ttl_minutes,
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
        return await self.lookup(tok) is not None

    async def lookup(self, tok: str) -> Optional[AuthToken]:
        """The valid, unused token (with its scope), or None; does not consume."""
        async with self._lock:
            self._cleanup()
            token = self._tokens.get(tok)
            return token if token is not None and token.is_valid() else None

    def _cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [k for k, v in self._tokens.items() if v.expires_at < now]
        for key in expired:
            self._tokens.pop(key, None)
