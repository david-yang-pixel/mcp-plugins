from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Optional, TypeVar

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.responses import Response

from .browser_relay import ZendeskBrowserRelay
from .coordination import TicketRunCoordinator
from .schemas import (
    AcceptCodexTicketResultRequest,
    BindCodexTicketJobRequest,
    CheckTicketRunRequest,
    ClaimTicketRunRequest,
    FinishTicketRunRequest,
    SendRequest,
)

logger = logging.getLogger("uvicorn.error")

MCP_PROTOCOL_VERSION = "2024-11-05"
CLAIM_TOOL_NAME = "claim_zendesk_ticket_run"
BIND_CODEX_JOB_TOOL_NAME = "bind_codex_ticket_job"
ACCEPT_CODEX_RESULT_TOOL_NAME = "accept_codex_ticket_result"
CHECK_TOOL_NAME = "check_zendesk_ticket_run"
FINISH_TOOL_NAME = "finish_zendesk_ticket_run"
SEND_TOOL_NAME = "send_zendesk_native_message"


TOOL_DEFINITIONS = [
    {
        "name": CLAIM_TOOL_NAME,
        "description": (
            "Acquire the single active worker lease for a Zendesk ticket. New "
            "webhook events are coalesced into the active run instead of starting "
            "another agent. The active Dify workflow may resume the same claim "
            "using its stable worker_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "event_id": {"type": "string", "minLength": 1},
                "worker_id": {"type": "string", "minLength": 1},
                "customer_last_message_id": {"type": "string"},
                "customer_message_count": {"type": "integer", "minimum": 0},
                "customer_requested_human": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "True only when the latest complete customer sequence "
                        "explicitly requests human support."
                    ),
                },
            },
            "required": [
                "ticket_id",
                "event_id",
                "customer_last_message_id",
                "customer_message_count",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": BIND_CODEX_JOB_TOOL_NAME,
        "description": (
            "Bind the one asynchronous Codex Runner job to the current ticket "
            "generation. Repeating the same binding is idempotent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "run_token": {"type": "string", "minLength": 16},
                "job_id": {"type": "string", "format": "uuid"},
            },
            "required": ["ticket_id", "run_token", "job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": ACCEPT_CODEX_RESULT_TOOL_NAME,
        "description": (
            "Accept a terminal Codex result only when its analyzed customer "
            "cursor equals the Ticket Actor's latest desired cursor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "run_token": {"type": "string", "minLength": 16},
                "job_id": {"type": "string", "format": "uuid"},
                "analyzed_customer_last_message_id": {"type": "string"},
                "analyzed_customer_message_count": {
                    "type": "integer",
                    "minimum": 0,
                },
            },
            "required": [
                "ticket_id",
                "run_token",
                "job_id",
                "analyzed_customer_last_message_id",
                "analyzed_customer_message_count",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": CHECK_TOOL_NAME,
        "description": (
            "Check that a ticket run is still the newest generation and that its "
            "customer-message snapshot has not changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "run_token": {"type": "string", "minLength": 16},
                "customer_last_message_id": {"type": "string"},
                "customer_message_count": {"type": "integer", "minimum": 0},
            },
            "required": [
                "ticket_id",
                "run_token",
                "customer_last_message_id",
                "customer_message_count",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": FINISH_TOOL_NAME,
        "description": (
            "Finish the current ticket run without sending, or record a terminal "
            "workflow outcome. A superseded run cannot finish the newer generation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer", "minimum": 1},
                "run_token": {"type": "string", "minLength": 16},
                "outcome": {
                    "type": "string",
                    "description": (
                        "Terminal outcome. Preferred values are sent, stale, "
                        "failed, no_reply, or superseded. A detailed failure "
                        "summary is normalized to failed."
                    ),
                },
            },
            "required": ["ticket_id", "run_token", "outcome"],
            "additionalProperties": False,
        },
    },
    {
        "name": SEND_TOOL_NAME,
        "description": (
            "Send a customer-visible Zendesk native messaging reply through the "
            "server-side browser relay. Pass run_token and the latest customer "
            "snapshot for generation-fenced Dify delivery."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Zendesk ticket ID.",
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 10000,
                    "description": "Message body to send to the customer.",
                },
                "run_token": {
                    "type": "string",
                    "minLength": 16,
                    "description": "Token returned by claim_zendesk_ticket_run.",
                },
                "handoff_type": {
                    "type": "string",
                    "enum": [
                        "none",
                        "support",
                        "billing",
                        "engineering",
                        "sre",
                        "security",
                        "privacy",
                    ],
                    "default": "none",
                    "description": (
                        "Set for an evidence-based specialist handoff. The "
                        "coordinator persists human ownership before delivery."
                    ),
                },
                "freshness_check_url": {
                    "type": "string",
                    "description": (
                        "Optional callback URL checked immediately before "
                        "clicking send."
                    ),
                },
                "freshness_check_api_key": {
                    "type": "string",
                    "description": "Optional Bearer key for freshness_check_url.",
                },
                "snapshot_customer_message_count": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Customer message count captured when reply was generated."
                    ),
                },
                "snapshot_customer_last_message_id": {
                    "type": "string",
                    "description": (
                        "Last customer message ID captured when reply was generated."
                    ),
                },
            },
            "required": [
                "ticket_id",
                "body",
                "run_token",
                "snapshot_customer_message_count",
                "snapshot_customer_last_message_id",
            ],
            "additionalProperties": False,
        },
    },
]


def sse_endpoint_response(endpoint: str = "/mcp") -> Response:
    return Response(
        f"event: endpoint\ndata: {endpoint}\n\n",
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def handle_mcp_request(
    payload: dict[str, Any],
    relay: ZendeskBrowserRelay,
    coordinator: TicketRunCoordinator,
) -> JSONResponse:
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    if payload.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _jsonrpc_error(request_id, -32600, "Invalid Request")

    if method == "initialize":
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "zendesk-browser-relay",
                    "version": "0.2.0",
                },
            },
        )

    if method == "ping":
        return _jsonrpc_result(request_id, {})

    if method == "notifications/initialized":
        return _jsonrpc_result(request_id, {})

    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": TOOL_DEFINITIONS})

    if method == "tools/call":
        return await _handle_tools_call(request_id, params, relay, coordinator)

    return _jsonrpc_error(request_id, -32601, "Method not found")


ModelT = TypeVar("ModelT", bound=BaseModel)


def _validate_arguments(
    request_id: Optional[Any],
    model: type[ModelT],
    arguments: Any,
) -> tuple[ModelT | None, JSONResponse | None]:
    if not isinstance(arguments, dict):
        return None, _jsonrpc_error(request_id, -32602, "Invalid tool arguments")
    try:
        return model(**arguments), None
    except ValidationError as exc:
        return None, _jsonrpc_error(
            request_id,
            -32602,
            "Invalid tool arguments",
            exc.errors(include_context=False),
        )


async def _handle_tools_call(
    request_id: Optional[Any],
    params: dict[str, Any],
    relay: ZendeskBrowserRelay,
    coordinator: TicketRunCoordinator,
) -> JSONResponse:
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}

    if tool_name == CLAIM_TOOL_NAME:
        request, error = _validate_arguments(
            request_id, ClaimTicketRunRequest, arguments
        )
        if error is not None:
            return error
        assert request is not None
        result = coordinator.claim(**request.model_dump())
        return _tool_result(
            request_id,
            asdict(result),
            (
                f"Ticket #{result.ticket_id} generation {result.generation}: "
                f"{result.reason}"
            ),
        )

    if tool_name == CHECK_TOOL_NAME:
        request, error = _validate_arguments(
            request_id, CheckTicketRunRequest, arguments
        )
        if error is not None:
            return error
        assert request is not None
        result = coordinator.check(**request.model_dump())
        return _tool_result(
            request_id,
            asdict(result),
            f"Ticket #{result.ticket_id} run check: {result.reason}",
        )

    if tool_name == BIND_CODEX_JOB_TOOL_NAME:
        request, error = _validate_arguments(
            request_id, BindCodexTicketJobRequest, arguments
        )
        if error is not None:
            return error
        assert request is not None
        result = coordinator.bind_codex_job(**request.model_dump())
        return _tool_result(
            request_id,
            asdict(result),
            f"Ticket #{result.ticket_id} Codex job binding: {result.reason}",
            is_error=not result.bound,
        )

    if tool_name == ACCEPT_CODEX_RESULT_TOOL_NAME:
        request, error = _validate_arguments(
            request_id, AcceptCodexTicketResultRequest, arguments
        )
        if error is not None:
            return error
        assert request is not None
        result = coordinator.accept_codex_result(**request.model_dump())
        return _tool_result(
            request_id,
            asdict(result),
            f"Ticket #{result.ticket_id} Codex result acceptance: {result.reason}",
            is_error=not result.accepted,
        )

    if tool_name == FINISH_TOOL_NAME:
        request, error = _validate_arguments(
            request_id, FinishTicketRunRequest, arguments
        )
        if error is not None:
            return error
        assert request is not None
        result = coordinator.finish(**request.model_dump())
        return _tool_result(
            request_id,
            asdict(result),
            f"Ticket #{result.ticket_id} run finish: {result.reason}",
        )

    if tool_name != SEND_TOOL_NAME:
        return _jsonrpc_error(request_id, -32602, "Unknown tool")

    send_request, error = _validate_arguments(request_id, SendRequest, arguments)
    if error is not None:
        return error
    assert send_request is not None

    if send_request.run_token is not None:
        assert send_request.snapshot_customer_last_message_id is not None
        assert send_request.snapshot_customer_message_count is not None
        if send_request.handoff_type != "none":
            locked = coordinator.mark_human_owned(
                ticket_id=send_request.ticket_id,
                run_token=send_request.run_token,
                handoff_type=send_request.handoff_type,
            )
            if not locked:
                return _tool_result(
                    request_id,
                    {
                        "ok": False,
                        "ticket_id": send_request.ticket_id,
                        "status": "handoff_lock_failed",
                    },
                    (
                        "Zendesk handoff was not sent for ticket "
                        f"#{send_request.ticket_id}: handoff_lock_failed"
                    ),
                    is_error=True,
                )
        authorization = coordinator.authorize_delivery(
            ticket_id=send_request.ticket_id,
            run_token=send_request.run_token,
            body=send_request.body,
            customer_last_message_id=(send_request.snapshot_customer_last_message_id),
            customer_message_count=send_request.snapshot_customer_message_count,
        )
        if not authorization.authorized:
            return _tool_result(
                request_id,
                {
                    "ok": False,
                    "ticket_id": send_request.ticket_id,
                    "status": authorization.reason,
                    "generation": authorization.generation,
                },
                (
                    f"Zendesk native message was not sent for ticket "
                    f"#{send_request.ticket_id}: {authorization.reason}"
                ),
                is_error=True,
            )

    try:
        result = await relay.send_message(send_request)
    except HTTPException as exc:
        delivery_result = None
        if send_request.run_token is not None:
            delivery_result = coordinator.record_delivery_result(
                ticket_id=send_request.ticket_id,
                run_token=send_request.run_token,
                sent=False,
                relay_status=f"http_exception:{exc.status_code}",
            )
        if delivery_result is not None and delivery_result.follow_up_required:
            return _follow_up_tool_result(
                request_id,
                send_request,
                delivery_result,
                status=f"http_exception:{exc.status_code}",
            )
        return _jsonrpc_error(request_id, -32000, str(exc.detail))
    except Exception as exc:  # noqa: BLE001  # pragma: no cover - boundary
        logger.exception(
            "zendesk_native_message_unhandled_exception ticket_id=%s error_type=%s",
            send_request.ticket_id,
            type(exc).__name__,
        )
        delivery_result = None
        if send_request.run_token is not None:
            delivery_result = coordinator.record_delivery_result(
                ticket_id=send_request.ticket_id,
                run_token=send_request.run_token,
                sent=False,
                relay_status=f"exception:{type(exc).__name__}",
            )
        if delivery_result is not None and delivery_result.follow_up_required:
            return _follow_up_tool_result(
                request_id,
                send_request,
                delivery_result,
                status=f"exception:{type(exc).__name__}",
            )
        return _jsonrpc_error(request_id, -32000, type(exc).__name__)

    delivery_result = None
    if send_request.run_token is not None:
        delivery_result = coordinator.record_delivery_result(
            ticket_id=send_request.ticket_id,
            run_token=send_request.run_token,
            sent=result.ok and result.status == "sent",
            relay_status=result.status,
        )

    structured = {
        "ok": result.ok,
        "ticket_id": result.ticket_id,
        "status": result.status,
        "current_url": result.current_url,
        "error": result.error,
    }
    if delivery_result is not None:
        structured.update(
            {
                "follow_up_required": delivery_result.follow_up_required,
                "next_generation": delivery_result.next_generation,
                "next_run_token": delivery_result.next_run_token,
                "customer_last_message_id": (
                    delivery_result.customer_last_message_id
                ),
                "customer_message_count": (
                    delivery_result.customer_message_count
                ),
            }
        )
    return _tool_result(
        request_id,
        structured,
        (
            f"Zendesk native message send status: {result.status} "
            f"for ticket #{result.ticket_id}"
        ),
        is_error=not result.ok and not bool(
            delivery_result and delivery_result.follow_up_required
        ),
    )


def _follow_up_tool_result(
    request_id: Optional[Any],
    send_request: SendRequest,
    delivery_result: Any,
    *,
    status: str,
) -> JSONResponse:
    """Return the new customer generation even when the old send is ambiguous."""
    return _tool_result(
        request_id,
        {
            "ok": False,
            "ticket_id": send_request.ticket_id,
            "status": status,
            "follow_up_required": True,
            "next_generation": delivery_result.next_generation,
            "next_run_token": delivery_result.next_run_token,
            "customer_last_message_id": delivery_result.customer_last_message_id,
            "customer_message_count": delivery_result.customer_message_count,
        },
        (
            f"Zendesk send result is ambiguous for ticket #{send_request.ticket_id}; "
            "a newer customer message must be processed with the returned next run token"
        ),
    )


def _tool_result(
    request_id: Optional[Any],
    structured: dict[str, Any],
    text: str,
    *,
    is_error: bool = False,
) -> JSONResponse:
    return _jsonrpc_result(
        request_id,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
            "isError": is_error,
        },
    )


def _jsonrpc_result(request_id: Optional[Any], result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _jsonrpc_error(
    request_id: Optional[Any],
    code: int,
    message: str,
    data: Any = None,
) -> JSONResponse:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error})
