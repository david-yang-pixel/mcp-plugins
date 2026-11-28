from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db_gateway.teleport_tokens import AuthToken, TokenStore


def test_auth_token_expires_after_five_minutes() -> None:
    token = AuthToken(
        token="tok",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=6),
    )

    assert token.is_valid() is False


@pytest.mark.asyncio
async def test_token_store_create_peek_consume_once() -> None:
    store = TokenStore()

    token = await store.create()

    assert await store.peek(token) is True
    assert await store.consume(token) is True
    assert await store.peek(token) is False
    assert await store.consume(token) is False


async def test_links_from_alerts_can_live_longer() -> None:
    store = TokenStore()
    token = await store.create(ttl_minutes=60)
    entry = await store.lookup(token)
    assert entry.expires_at - entry.created_at == timedelta(minutes=60)
    assert await store.lookup("unknown") is None
    with pytest.raises(ValueError):
        await store.create(ttl_minutes=0)
