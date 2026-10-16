from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from starlette.responses import Response

from .auth import require_api_key, require_codex_completion_api_key
from .browser_relay import ZendeskBrowserRelay
from .coordination import CodexContinuation, TicketRunCoordinator
from .mcp import handle_mcp_request, sse_endpoint_response
from .feishu_notifications import FeishuStatusNotifier
from .result_dispatch import (
    CodexContinuationDispatcher,
    ContinuationDispatchError,
    DifyResultDispatcher,
    ResultDispatchError,
)
from .schemas import (
    CodexCompletionRequest,
    CodexCompletionResponse,
    HealthResponse,
    ReadinessResponse,
    SendRequest,
    SendResponse,
    SessionResponse,
)
from .settings import get_settings
from .session_supervisor import run_session_supervisor
from .zendesk_freshness import ZendeskMcpFreshnessChecker

_relay_singleton: ZendeskBrowserRelay | None = None
_coordinator_singleton: TicketRunCoordinator | None = None
_result_dispatcher_singleton: DifyResultDispatcher | None = None
_continuation_dispatcher_singleton: CodexContinuationDispatcher | None = None
_freshness_checker_singleton: ZendeskMcpFreshnessChecker | None = None


def get_relay() -> ZendeskBrowserRelay:
    global _relay_singleton
    if _relay_singleton is None:
        _relay_singleton = ZendeskBrowserRelay(
            get_settings(),
            coordinator=get_coordinator(),
            freshness_checker=get_freshness_checker(),
            feishu_notifier=get_feishu_notifier(),
        )
    return _relay_singleton


def get_feishu_notifier() -> FeishuStatusNotifier | None:
    settings = get_settings()
    webhook_url = settings.feishu_notification_webhook_url
    if not webhook_url:
        return None
    try:
        return FeishuStatusNotifier(webhook_url)
    except ValueError:
        return None


def get_coordinator() -> TicketRunCoordinator:
    global _coordinator_singleton
    if _coordinator_singleton is None:
        settings = get_settings()
        _coordinator_singleton = TicketRunCoordinator(
            settings.ticket_run_db_path,
            active_lease_seconds=settings.ticket_run_active_lease_seconds,
            coalesce_active_customer_messages=(
                settings.ticket_run_coalesce_active_messages
            ),
            ticket_allowlist=settings.ticket_automation_allowlist_values,
        )
    return _coordinator_singleton


def get_result_dispatcher() -> DifyResultDispatcher | None:
    global _result_dispatcher_singleton
    if _result_dispatcher_singleton is None:
        settings = get_settings()
        api_key = settings.dify_result_workflow_api_key_value
        if not settings.dify_result_workflow_url or not api_key:
            return None
        _result_dispatcher_singleton = DifyResultDispatcher(
            workflow_url=settings.dify_result_workflow_url,
            api_key=api_key,
            user=settings.dify_result_workflow_user,
        )
    return _result_dispatcher_singleton


def get_continuation_dispatcher() -> CodexContinuationDispatcher | None:
    global _continuation_dispatcher_singleton
    if _continuation_dispatcher_singleton is None:
        settings = get_settings()
        api_key = settings.codex_runner_api_key_value
        if not settings.codex_runner_create_url or not api_key:
            return None
        _continuation_dispatcher_singleton = CodexContinuationDispatcher(
            runner_url=settings.codex_runner_create_url,
            token=api_key,
        )
    return _continuation_dispatcher_singleton


def get_freshness_checker() -> ZendeskMcpFreshnessChecker | None:
    global _freshness_checker_singleton
    if _freshness_checker_singleton is None:
        settings = get_settings()
        authorization = settings.zendesk_mcp_authorization_value
        if not settings.zendesk_mcp_url or not authorization:
            return None
        _freshness_checker_singleton = ZendeskMcpFreshnessChecker(
            endpoint_url=settings.zendesk_mcp_url,
            authorization=authorization,
        )
    return _freshness_checker_singleton


def reset_relay_for_tests() -> None:
    global _relay_singleton, _coordinator_singleton
    global _result_dispatcher_singleton, _continuation_dispatcher_singleton
    global _freshness_checker_singleton
    _relay_singleton = None
    _coordinator_singleton = None
    _result_dispatcher_singleton = None
    _continuation_dispatcher_singleton = None
    _freshness_checker_singleton = None


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        supervisor_task: asyncio.Task[None] | None = None
        settings = get_settings()
        if (
            settings.zendesk_session_supervisor_enabled
            and settings.auto_login_configured
        ):
            supervisor_task = asyncio.create_task(
                run_session_supervisor(
                    get_relay(),
                    interval_seconds=(
                        settings.zendesk_session_check_interval_seconds
                    ),
                ),
                name="zendesk-session-supervisor",
            )
        try:
            yield
        finally:
            if supervisor_task is not None:
                supervisor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await supervisor_task
            if _relay_singleton is not None:
                await _relay_singleton.close()

    app = FastAPI(title="Zendesk Browser Relay", version="0.2.0", lifespan=lifespan)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.post(
        "/v1/codex-completions",
        response_model=CodexCompletionResponse,
        status_code=202,
        dependencies=[Depends(require_codex_completion_api_key)],
    )
    async def codex_completion(
        payload: CodexCompletionRequest,
        coordinator: TicketRunCoordinator = Depends(get_coordinator),
        dispatcher: DifyResultDispatcher | None = Depends(get_result_dispatcher),
        continuation_dispatcher: CodexContinuationDispatcher | None = Depends(
            get_continuation_dispatcher
        ),
    ) -> CodexCompletionResponse:
        async def start_continuation(
            continuation: CodexContinuation,
        ) -> CodexCompletionResponse:
            if not continuation.prepared:
                return CodexCompletionResponse(
                    accepted=False,
                    reason=continuation.reason,
                )
            if continuation_dispatcher is None:
                raise HTTPException(
                    status_code=503,
                    detail={"error": "continuation_dispatch_disabled"},
                )
            try:
                next_job_id = await continuation_dispatcher.start(
                    continuation.ticket_id,
                    continuation.request_id,
                )
            except ContinuationDispatchError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"error": "continuation_dispatch_failed"},
                ) from exc
            binding = coordinator.bind_codex_job(
                ticket_id=continuation.ticket_id,
                run_token=continuation.run_token,
                job_id=next_job_id,
            )
            if not binding.bound:
                raise HTTPException(
                    status_code=409,
                    detail={"error": binding.reason},
                )
            return CodexCompletionResponse(
                accepted=False,
                reason="continuation_started",
                dirty=False,
            )

        run_token, separator, cursor_suffix = payload.request_id.rpartition(":")
        if (
            not separator
            or len(run_token) < 16
            or not cursor_suffix
            or not cursor_suffix.isdigit()
        ):
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_actor_request_id"},
            )
        existing_continuation = coordinator.continuation_for_source(
            ticket_id=payload.ticket_id,
            source_job_id=payload.job_id,
        )
        if existing_continuation is not None:
            return await start_continuation(existing_continuation)
        binding = coordinator.bind_codex_job(
            ticket_id=payload.ticket_id,
            run_token=run_token,
            job_id=payload.job_id,
        )
        if not binding.bound:
            if binding.reason == "job_conflict":
                raise HTTPException(
                    status_code=409,
                    detail={"error": "job_conflict"},
                )
            return CodexCompletionResponse(
                accepted=False,
                reason=binding.reason,
            )
        if payload.status != "succeeded":
            accepted = coordinator.accept_codex_failure_for_bound_job(
                ticket_id=payload.ticket_id,
                job_id=payload.job_id,
                status=payload.status,
            )
            if accepted.reason == "job_not_bound":
                raise HTTPException(status_code=409, detail={"error": "job_not_bound"})
            if not accepted.accepted:
                return CodexCompletionResponse(
                    accepted=False,
                    reason=accepted.reason,
                    dirty=accepted.dirty,
                )
            if accepted.dirty:
                continuation = coordinator.prepare_dirty_continuation(
                    ticket_id=payload.ticket_id,
                    source_job_id=payload.job_id,
                )
                return await start_continuation(continuation)
            if accepted.reason == "already_dispatched":
                return CodexCompletionResponse(
                    accepted=True,
                    reason=accepted.reason,
                    dirty=accepted.dirty,
                    dispatched=True,
                )
            if dispatcher is None:
                raise HTTPException(
                    status_code=503,
                    detail={"error": "dify_result_dispatch_disabled"},
                )
            try:
                await dispatcher.dispatch(payload, accepted)
            except ResultDispatchError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"error": "dify_result_dispatch_failed"},
                ) from exc
            coordinator.mark_dify_result_dispatched(
                ticket_id=payload.ticket_id,
                run_token=accepted.run_token,
                job_id=payload.job_id,
            )
            return CodexCompletionResponse(
                accepted=True,
                reason=accepted.reason,
                dirty=accepted.dirty,
                dispatched=True,
            )
        assert payload.data is not None
        if payload.data.get("ticket_id") != payload.ticket_id:
            raise HTTPException(status_code=422, detail={"error": "ticket_mismatch"})
        message_id = payload.data.get("analyzed_customer_last_message_id")
        message_count = payload.data.get("analyzed_customer_message_count")
        if not isinstance(message_id, str) or not isinstance(message_count, int):
            raise HTTPException(status_code=422, detail={"error": "missing_cursor"})
        accepted = coordinator.accept_codex_result_for_bound_job(
            ticket_id=payload.ticket_id,
            job_id=payload.job_id,
            analyzed_customer_last_message_id=message_id,
            analyzed_customer_message_count=message_count,
        )
        if accepted.reason == "job_not_bound":
            raise HTTPException(status_code=409, detail={"error": "job_not_bound"})
        if not accepted.accepted:
            if accepted.reason == "customer_snapshot_changed" and accepted.dirty:
                continuation = coordinator.prepare_dirty_continuation(
                    ticket_id=payload.ticket_id,
                    source_job_id=payload.job_id,
                )
                return await start_continuation(continuation)
            return CodexCompletionResponse(
                accepted=False,
                reason=accepted.reason,
                dirty=accepted.dirty,
            )
        if accepted.reason == "already_dispatched":
            return CodexCompletionResponse(
                accepted=True,
                reason=accepted.reason,
                dirty=False,
                dispatched=True,
            )
        if dispatcher is None:
            raise HTTPException(
                status_code=503,
                detail={"error": "dify_result_dispatch_disabled"},
            )
        try:
            await dispatcher.dispatch(payload, accepted)
        except ResultDispatchError as exc:
            raise HTTPException(
                status_code=503,
                detail={"error": "dify_result_dispatch_failed"},
            ) from exc
        coordinator.mark_dify_result_dispatched(
            ticket_id=payload.ticket_id,
            run_token=accepted.run_token,
            job_id=payload.job_id,
        )
        return CodexCompletionResponse(
            accepted=True,
            reason=accepted.reason,
            dirty=False,
            dispatched=True,
        )

    @app.get(
        "/session",
        response_model=SessionResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def session(
        current_relay: ZendeskBrowserRelay = Depends(get_relay),
    ) -> SessionResponse:
        return await current_relay.check_session()

    @app.get(
        "/ready",
        response_model=ReadinessResponse,
        dependencies=[Depends(require_api_key)],
    )
    async def ready(
        current_relay: ZendeskBrowserRelay = Depends(get_relay),
    ) -> Response:
        result = await current_relay.check_readiness()
        payload = ReadinessResponse(ok=result.ok, status=result.status)
        return JSONResponse(
            status_code=200 if result.ok else 503,
            content=payload.model_dump(),
        )

    @app.post(
        "/send", response_model=SendResponse, dependencies=[Depends(require_api_key)]
    )
    async def send(
        payload: SendRequest,
        current_relay: ZendeskBrowserRelay = Depends(get_relay),
    ) -> SendResponse:
        return await current_relay.send_message(payload)

    @app.post("/mcp", response_model=None, dependencies=[Depends(require_api_key)])
    async def mcp(
        payload: dict,
        current_relay: ZendeskBrowserRelay = Depends(get_relay),
        coordinator: TicketRunCoordinator = Depends(get_coordinator),
    ) -> JSONResponse:
        return await handle_mcp_request(payload, current_relay, coordinator)

    @app.get("/sse", response_model=None, dependencies=[Depends(require_api_key)])
    async def sse(x_mcp_endpoint: str = Header(default="/mcp")) -> Response:
        return sse_endpoint_response(x_mcp_endpoint)

    return app


app = create_app()


def main() -> None:
    uvicorn.run("zendesk_browser_relay.app:app", host="127.0.0.1", port=8097)


if __name__ == "__main__":
    main()
