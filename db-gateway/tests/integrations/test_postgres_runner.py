from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from db_gateway.postgres import run_psql


@pytest.mark.asyncio
async def test_run_psql_kills_and_reaps_process_on_timeout():
    class FakeProcess:
        def __init__(self) -> None:
            self.killed = False
            self.communicate_calls = 0

        async def communicate(self):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                await asyncio.sleep(1)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

    fake_proc = FakeProcess()

    with patch(
        "db_gateway.postgres.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake_proc),
    ):
        with pytest.raises(asyncio.TimeoutError):
            await run_psql(
                host="localhost",
                port=56061,
                user="read",
                password="pw",
                database="langgenius",
                sql="SELECT 1",
                timeout=0.01,
                max_rows=10,
            )

    assert fake_proc.killed is True
    assert fake_proc.communicate_calls == 2
