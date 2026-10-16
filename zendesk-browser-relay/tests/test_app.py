from fastapi.testclient import TestClient
from threading import Event


class FakeRelay:
    async def check_session(self):
        from zendesk_browser_relay.schemas import SessionResponse

        return SessionResponse(ok=True, status="logged_in", current_url="https://langgenius.zendesk.com/agent/dashboard")

    async def send_message(self, request):
        from zendesk_browser_relay.schemas import SendResponse

        return SendResponse(ok=True, ticket_id=request.ticket_id, status="sent", current_url="https://langgenius.zendesk.com/agent/tickets/2861")

    async def check_readiness(self):
        from zendesk_browser_relay.schemas import SessionResponse

        return SessionResponse(ok=True, status="logged_in")


class LoginRequiredRelay(FakeRelay):
    async def check_readiness(self):
        from zendesk_browser_relay.schemas import SessionResponse

        return SessionResponse(ok=False, status="login_required")


class FakeResultDispatcher:
    def __init__(self):
        self.payloads = []

    async def dispatch(self, payload, acceptance):
        self.payloads.append((payload, acceptance))


class FakeContinuationDispatcher:
    def __init__(self):
        self.requests = []

    async def start(self, ticket_id, request_id):
        self.requests.append((ticket_id, request_id))
        return "22222222-2222-4222-8222-222222222222"


def test_health_does_not_require_auth(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    from zendesk_browser_relay.app import create_app
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "zendesk-browser-relay"}


def test_lifespan_starts_independent_session_supervisor_when_login_is_configured(
    monkeypatch,
):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    monkeypatch.setenv("ZENDESK_LOGIN_EMAIL", "support@example.com")
    monkeypatch.setenv("ZENDESK_LOGIN_PASSWORD", "secret-password")
    from zendesk_browser_relay import app as app_module
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    started = Event()
    relay = object()

    async def supervisor(current_relay, *, interval_seconds):
        assert current_relay is relay
        assert interval_seconds == 30
        started.set()
        import asyncio

        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "get_relay", lambda: relay)
    monkeypatch.setattr(app_module, "run_session_supervisor", supervisor)

    with TestClient(app_module.create_app()):
        assert started.wait(timeout=1)


def test_get_relay_reuses_singleton(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    monkeypatch.setenv("TICKET_RUN_DB_PATH", str(tmp_path / "ticket-runs.sqlite3"))
    from zendesk_browser_relay.app import get_relay, reset_relay_for_tests
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    reset_relay_for_tests()

    first = get_relay()
    second = get_relay()

    assert first is second


def test_session_returns_relay_status(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    from zendesk_browser_relay.app import create_app, get_relay
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_relay] = lambda: FakeRelay()
    client = TestClient(app)

    response = client.get("/session", headers={"Authorization": "Bearer " + "x" * 32})

    assert response.status_code == 200
    assert response.json()["status"] == "logged_in"


def test_readiness_is_authenticated_and_returns_503_when_login_is_required(
    monkeypatch,
):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    from zendesk_browser_relay.app import create_app, get_relay
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_relay] = lambda: LoginRequiredRelay()
    client = TestClient(app)

    unauthorized = client.get("/ready")
    response = client.get(
        "/ready", headers={"Authorization": "Bearer " + "x" * 32}
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 503
    assert response.json() == {"ok": False, "status": "login_required"}


def test_send_returns_relay_result(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    from zendesk_browser_relay.app import create_app, get_relay
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_relay] = lambda: FakeRelay()
    client = TestClient(app)

    response = client.post(
        "/send",
        headers={"Authorization": "Bearer " + "x" * 32},
        json={"ticket_id": 2861, "body": "hello"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert response.json()["ticket_id"] == 2861


def test_codex_completion_callback_requires_its_own_key_and_fences_cursor(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    monkeypatch.setenv("CODEX_COMPLETION_API_KEY", "c" * 32)
    from zendesk_browser_relay.app import create_app, get_coordinator
    from zendesk_browser_relay.coordination import TicketRunCoordinator
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    coordinator = TicketRunCoordinator(
        tmp_path / "ticket-runs.sqlite3",
        coalesce_active_customer_messages=True,
    )
    claim = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    job_id = "11111111-1111-4111-8111-111111111111"
    assert coordinator.bind_codex_job(
        ticket_id=3880, run_token=claim.run_token, job_id=job_id
    ).bound
    coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )
    app = create_app()
    app.dependency_overrides[get_coordinator] = lambda: coordinator
    client = TestClient(app)
    payload = {
        "ticket_id": 3880,
        "request_id": f"{claim.run_token}:1",
        "job_id": job_id,
        "status": "succeeded",
        "data": {
            "outcome": "reply",
            "ticket_id": 3880,
            "analyzed_customer_last_message_id": "message-1",
            "analyzed_customer_message_count": 1,
            "reply_body": "Hi,\n\nAnswer.\n\nBest regards,\nDify Support",
            "handoff_type": "none",
            "internal_summary": "summary",
            "evidence": [],
            "unknowns": [],
        },
        "meta": {"elapsed_ms": 42},
    }

    unauthorized = client.post("/v1/codex-completions", json=payload)
    response = client.post(
        "/v1/codex-completions",
        headers={"Authorization": "Bearer " + "c" * 32},
        json=payload,
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "continuation_dispatch_disabled"


def test_current_codex_completion_triggers_the_short_dify_result_stage(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    monkeypatch.setenv("CODEX_COMPLETION_API_KEY", "c" * 32)
    from zendesk_browser_relay.app import (
        create_app,
        get_coordinator,
        get_result_dispatcher,
    )
    from zendesk_browser_relay.coordination import TicketRunCoordinator
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    coordinator = TicketRunCoordinator(tmp_path / "ticket-runs.sqlite3")
    claim = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    job_id = "11111111-1111-4111-8111-111111111111"
    assert coordinator.bind_codex_job(
        ticket_id=3880, run_token=claim.run_token, job_id=job_id
    ).bound
    dispatcher = FakeResultDispatcher()
    app = create_app()
    app.dependency_overrides[get_coordinator] = lambda: coordinator
    app.dependency_overrides[get_result_dispatcher] = lambda: dispatcher
    client = TestClient(app)
    payload = {
        "ticket_id": 3880,
        "request_id": f"{claim.run_token}:1",
        "job_id": job_id,
        "status": "succeeded",
        "data": {
            "outcome": "reply",
            "ticket_id": 3880,
            "analyzed_customer_last_message_id": "message-1",
            "analyzed_customer_message_count": 1,
            "reply_body": "Hi,\n\nAnswer.\n\nBest regards,\nDify Support",
            "handoff_type": "none",
            "internal_summary": "summary",
            "evidence": [],
            "unknowns": [],
        },
        "meta": {"elapsed_ms": 42},
    }

    response = client.post(
        "/v1/codex-completions",
        headers={"Authorization": "Bearer " + "c" * 32},
        json=payload,
    )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert response.json()["dispatched"] is True
    assert len(dispatcher.payloads) == 1
    assert dispatcher.payloads[0][1].run_token == claim.run_token

    repeated = client.post(
        "/v1/codex-completions",
        headers={"Authorization": "Bearer " + "c" * 32},
        json=payload,
    )
    assert repeated.status_code == 202
    assert repeated.json()["reason"] == "already_dispatched"
    assert len(dispatcher.payloads) == 1


def test_codex_timeout_triggers_a_no_send_dify_result_stage(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    monkeypatch.setenv("CODEX_COMPLETION_API_KEY", "c" * 32)
    from zendesk_browser_relay.app import (
        create_app,
        get_coordinator,
        get_result_dispatcher,
    )
    from zendesk_browser_relay.coordination import TicketRunCoordinator
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    coordinator = TicketRunCoordinator(tmp_path / "ticket-runs.sqlite3")
    claim = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    job_id = "11111111-1111-4111-8111-111111111111"
    assert coordinator.bind_codex_job(
        ticket_id=3880, run_token=claim.run_token, job_id=job_id
    ).bound
    dispatcher = FakeResultDispatcher()
    app = create_app()
    app.dependency_overrides[get_coordinator] = lambda: coordinator
    app.dependency_overrides[get_result_dispatcher] = lambda: dispatcher
    client = TestClient(app)

    response = client.post(
        "/v1/codex-completions",
        headers={"Authorization": "Bearer " + "c" * 32},
        json={
            "ticket_id": 3880,
            "request_id": f"{claim.run_token}:1",
            "job_id": job_id,
            "status": "timed_out",
            "error": "analysis_timeout",
        },
    )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert response.json()["dispatched"] is True
    assert len(dispatcher.payloads) == 1
    assert dispatcher.payloads[0][0].status == "timed_out"
    assert dispatcher.payloads[0][1].run_token == claim.run_token

    repeated = client.post(
        "/v1/codex-completions",
        headers={"Authorization": "Bearer " + "c" * 32},
        json={
            "ticket_id": 3880,
            "request_id": f"{claim.run_token}:1",
            "job_id": job_id,
            "status": "timed_out",
            "error": "analysis_timeout",
        },
    )
    assert repeated.status_code == 202
    assert repeated.json()["reason"] == "already_dispatched"
    assert len(dispatcher.payloads) == 1


def test_completion_callback_atomically_binds_the_runner_job_from_request_id(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    monkeypatch.setenv("CODEX_COMPLETION_API_KEY", "c" * 32)
    from zendesk_browser_relay.app import (
        create_app,
        get_coordinator,
        get_result_dispatcher,
    )
    from zendesk_browser_relay.coordination import TicketRunCoordinator
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    coordinator = TicketRunCoordinator(tmp_path / "ticket-runs.sqlite3")
    claim = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    dispatcher = FakeResultDispatcher()
    app = create_app()
    app.dependency_overrides[get_coordinator] = lambda: coordinator
    app.dependency_overrides[get_result_dispatcher] = lambda: dispatcher
    client = TestClient(app)
    job_id = "11111111-1111-4111-8111-111111111111"

    response = client.post(
        "/v1/codex-completions",
        headers={"Authorization": "Bearer " + "c" * 32},
        json={
            "ticket_id": 3880,
            "request_id": f"{claim.run_token}:1",
            "job_id": job_id,
            "status": "succeeded",
            "data": {
                "outcome": "no_send",
                "ticket_id": 3880,
                "analyzed_customer_last_message_id": "message-1",
                "analyzed_customer_message_count": 1,
                "reply_body": "",
                "handoff_type": "none",
                "internal_summary": "No customer issue supplied.",
                "evidence": [],
                "unknowns": [],
            },
        },
    )

    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert response.json()["dispatched"] is True
    assert len(dispatcher.payloads) == 1


def test_stale_codex_completion_starts_one_latest_cursor_continuation(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("RELAY_API_KEY", "x" * 32)
    monkeypatch.setenv("CODEX_COMPLETION_API_KEY", "c" * 32)
    from zendesk_browser_relay.app import (
        create_app,
        get_continuation_dispatcher,
        get_coordinator,
        get_result_dispatcher,
    )
    from zendesk_browser_relay.coordination import TicketRunCoordinator
    from zendesk_browser_relay.settings import get_settings

    get_settings.cache_clear()
    coordinator = TicketRunCoordinator(
        tmp_path / "ticket-runs.sqlite3",
        coalesce_active_customer_messages=True,
    )
    first = coordinator.claim(
        ticket_id=3880,
        event_id="message-1",
        customer_last_message_id="message-1",
        customer_message_count=1,
    )
    old_job_id = "11111111-1111-4111-8111-111111111111"
    assert coordinator.bind_codex_job(
        ticket_id=3880,
        run_token=first.run_token,
        job_id=old_job_id,
    ).bound
    coordinator.claim(
        ticket_id=3880,
        event_id="message-2",
        customer_last_message_id="message-2",
        customer_message_count=2,
    )
    result_dispatcher = FakeResultDispatcher()
    continuation_dispatcher = FakeContinuationDispatcher()
    app = create_app()
    app.dependency_overrides[get_coordinator] = lambda: coordinator
    app.dependency_overrides[get_result_dispatcher] = lambda: result_dispatcher
    app.dependency_overrides[get_continuation_dispatcher] = (
        lambda: continuation_dispatcher
    )
    client = TestClient(app)
    payload = {
        "ticket_id": 3880,
        "request_id": f"{first.run_token}:1",
        "job_id": old_job_id,
        "status": "succeeded",
        "data": {
            "outcome": "no_send",
            "ticket_id": 3880,
            "analyzed_customer_last_message_id": "message-1",
            "analyzed_customer_message_count": 1,
            "reply_body": "",
            "handoff_type": "none",
            "internal_summary": "stale",
            "evidence": [],
            "unknowns": [],
        },
    }

    response = client.post(
        "/v1/codex-completions",
        headers={"Authorization": "Bearer " + "c" * 32},
        json=payload,
    )

    assert response.status_code == 202
    assert response.json()["accepted"] is False
    assert response.json()["reason"] == "continuation_started"
    assert len(result_dispatcher.payloads) == 0
    assert len(continuation_dispatcher.requests) == 1
    ticket_id, request_id = continuation_dispatcher.requests[0]
    assert ticket_id == 3880
    assert request_id.endswith(":2")
    current = coordinator.continuation_for_source(
        ticket_id=3880, source_job_id=old_job_id
    )
    assert current is not None
    assert coordinator.bind_codex_job(
        ticket_id=3880,
        run_token=current.run_token,
        job_id="22222222-2222-4222-8222-222222222222",
    ).reason == "already_bound"
