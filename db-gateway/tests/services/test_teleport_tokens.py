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


async def test_tokens_carry_the_session_they_renew() -> None:
    from db_gateway.teleport_tokens import SCOPE_HOST, SCOPE_SESSION

    store = TokenStore()
    own = await store.create()
    host = await store.create(SCOPE_HOST)

    assert (await store.lookup(own)).scope == SCOPE_SESSION
    assert (await store.lookup(host)).scope == SCOPE_HOST
    assert await store.consume(host) is True
    assert await store.lookup(host) is None
    assert await store.lookup("unknown") is None
    with pytest.raises(ValueError):
        await store.create("other")
