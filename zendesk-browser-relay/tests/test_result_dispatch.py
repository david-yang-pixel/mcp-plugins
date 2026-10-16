import json

import pytest

from zendesk_browser_relay.coordination import CodexResultAcceptance
from zendesk_browser_relay.result_dispatch import DifyResultDispatcher
from zendesk_browser_relay.schemas import CodexCompletionRequest


@pytest.mark.asyncio
async def test_successful_completion_uses_nonempty_codex_error_input(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"data":{"status":"succeeded"}}'

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["user_agent"] = request.get_header("User-agent")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("zendesk_browser_relay.result_dispatch.urlopen", fake_urlopen)
    dispatcher = DifyResultDispatcher(
        workflow_url="http://127.0.0.1:9000/v1/workflows/run",
        api_key="a" * 32,
    )
    payload = CodexCompletionRequest(
        ticket_id=3880,
        request_id="r" * 32 + ":1",
        job_id="11111111-1111-4111-8111-111111111111",
        status="succeeded",
        data={
            "ticket_id": 3880,
            "analyzed_customer_last_message_id": "message-1",
            "analyzed_customer_message_count": 1,
        },
    )
    acceptance = CodexResultAcceptance(
        ticket_id=3880,
        generation=1,
        run_token="r" * 32,
        job_id=payload.job_id,
        accepted=True,
        reason="accepted",
        dirty=False,
        customer_last_message_id="message-1",
        customer_message_count=1,
    )

    await dispatcher.dispatch(payload, acceptance)

    assert captured["body"]["inputs"]["codex_error"] == "none"
    assert captured["body"]["inputs"]["codex_status"] == "succeeded"
    assert captured["user_agent"] == "Dify-Zendesk-Relay/1.0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ticket_id", "generation_message_count", "analyzed_message_count"),
    [
        (4049, 1, 6),
        (4051, 1, 2),
    ],
)
async def test_follow_up_dispatch_uses_analyzed_cumulative_message_count(
    monkeypatch,
    ticket_id,
    generation_message_count,
    analyzed_message_count,
):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b'{"data":{"status":"succeeded"}}'

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("zendesk_browser_relay.result_dispatch.urlopen", fake_urlopen)
    dispatcher = DifyResultDispatcher(
        workflow_url="http://127.0.0.1:9000/v1/workflows/run",
        api_key="a" * 32,
    )
    payload = CodexCompletionRequest(
        ticket_id=ticket_id,
        request_id="r" * 32 + f":{generation_message_count}",
        job_id="11111111-1111-4111-8111-111111111111",
        status="succeeded",
        data={
            "ticket_id": ticket_id,
            "analyzed_customer_last_message_id": "latest-message",
            "analyzed_customer_message_count": analyzed_message_count,
        },
    )
    acceptance = CodexResultAcceptance(
        ticket_id=ticket_id,
        generation=2,
        run_token="r" * 32,
        job_id=payload.job_id,
        accepted=True,
        reason="accepted",
        dirty=False,
        customer_last_message_id="latest-message",
        customer_message_count=generation_message_count,
    )

    await dispatcher.dispatch(payload, acceptance)

    assert captured["body"]["inputs"]["customer_message_count"] == str(
        analyzed_message_count
    )
