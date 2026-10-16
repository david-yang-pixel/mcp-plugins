from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .customer_reply import format_customer_reply

SessionStatus = Literal["logged_in", "login_required", "busy", "unknown"]
SendStatus = Literal[
    "sent",
    "login_required",
    "send_failed",
    "send_not_confirmed",
    "stale_context",
    "freshness_check_failed",
]


class HealthResponse(BaseModel):
    ok: bool = True
    service: str = "zendesk-browser-relay"


class CodexCompletionRequest(BaseModel):
    ticket_id: int = Field(..., gt=0)
    request_id: str = Field(
        ...,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    job_id: str = Field(
        ...,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$",
    )
    status: Literal["succeeded", "failed", "timed_out"]
    data: Optional[dict[str, Any]] = None
    meta: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "CodexCompletionRequest":
        if self.status == "succeeded" and self.data is None:
            raise ValueError("succeeded completion requires data")
        if self.status != "succeeded" and self.data is not None:
            raise ValueError("failed completion must not include data")
        return self


class CodexCompletionResponse(BaseModel):
    ok: bool = True
    accepted: bool
    reason: str
    dirty: bool = False
    dispatched: bool = False


class SessionResponse(BaseModel):
    ok: bool
    status: SessionStatus
    current_url: Optional[str] = None


class ReadinessResponse(BaseModel):
    ok: bool
    status: SessionStatus


class SendRequest(BaseModel):
    ticket_id: int = Field(..., gt=0)
    body: str = Field(..., min_length=1, max_length=10_000)
    freshness_check_url: Optional[str] = None
    freshness_check_api_key: Optional[str] = None
    snapshot_customer_message_count: Optional[int] = Field(default=None, ge=0)
    snapshot_customer_last_message_id: Optional[str] = None
    run_token: Optional[str] = Field(default=None, min_length=16, max_length=256)
    handoff_type: Literal[
        "none",
        "support",
        "billing",
        "engineering",
        "sre",
        "security",
        "privacy",
    ] = "none"

    @model_validator(mode="before")
    @classmethod
    def extract_internal_handoff_marker(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        body = data.get("body")
        marker_prefix = "[[DIFY_HANDOFF:"
        if not isinstance(body, str) or not body.startswith(marker_prefix):
            return data
        marker_end = body.find("]]\n")
        if marker_end < 0:
            raise ValueError("invalid internal handoff marker")
        marker_type = body[len(marker_prefix):marker_end]
        allowed = {
            "support",
            "billing",
            "engineering",
            "sre",
            "security",
            "privacy",
        }
        if marker_type not in allowed:
            raise ValueError("invalid internal handoff marker")
        explicit_type = str(data.get("handoff_type") or "none")
        if explicit_type not in {"none", marker_type}:
            raise ValueError("handoff marker conflicts with handoff_type")
        stripped_body = body[marker_end + 3:]
        if not stripped_body.strip():
            raise ValueError("handoff marker requires a message body")
        data["body"] = stripped_body
        data["handoff_type"] = marker_type
        return data

    @field_validator("body")
    @classmethod
    def normalize_customer_reply(cls, value: str) -> str:
        return format_customer_reply(value)

    @model_validator(mode="after")
    def require_snapshot_for_coordinated_delivery(self) -> "SendRequest":
        if self.run_token is not None and (
            self.snapshot_customer_message_count is None
            or self.snapshot_customer_last_message_id is None
        ):
            raise ValueError(
                "run_token requires snapshot_customer_message_count and "
                "snapshot_customer_last_message_id"
            )
        if self.handoff_type != "none" and self.run_token is None:
            raise ValueError("handoff_type requires run_token")
        return self


class SendResponse(BaseModel):
    ok: bool
    ticket_id: int
    status: SendStatus
    current_url: Optional[str] = None
    error: Optional[str] = None


class ClaimTicketRunRequest(BaseModel):
    ticket_id: int = Field(..., gt=0)
    event_id: str = Field(..., min_length=1, max_length=512)
    worker_id: str = Field(default="", max_length=512)
    customer_last_message_id: str = Field(default="", max_length=512)
    customer_message_count: int = Field(..., ge=0)
    customer_requested_human: bool = False


class CheckTicketRunRequest(BaseModel):
    ticket_id: int = Field(..., gt=0)
    run_token: str = Field(..., min_length=16, max_length=256)
    customer_last_message_id: str = Field(default="", max_length=512)
    customer_message_count: int = Field(..., ge=0)


class BindCodexTicketJobRequest(BaseModel):
    ticket_id: int = Field(..., gt=0)
    run_token: str = Field(..., min_length=16, max_length=256)
    job_id: str = Field(
        ...,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$",
    )


class AcceptCodexTicketResultRequest(BindCodexTicketJobRequest):
    analyzed_customer_last_message_id: str = Field(default="", max_length=512)
    analyzed_customer_message_count: int = Field(..., ge=0)


RunOutcome = Literal[
    "sent",
    "stale",
    "failed",
    "no_reply",
    "superseded",
    "human",
    "handoff_support",
    "handoff_billing",
    "handoff_engineering",
    "handoff_sre",
    "handoff_security",
    "handoff_privacy",
    "manual_reconciliation",
]


class FinishTicketRunRequest(BaseModel):
    ticket_id: int = Field(..., gt=0)
    run_token: str = Field(..., min_length=16, max_length=256)
    outcome: RunOutcome

    @field_validator("outcome", mode="before")
    @classmethod
    def normalize_agent_outcome(cls, value: object) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {
            "sent",
            "stale",
            "failed",
            "no_reply",
            "superseded",
            "human",
            "handoff_support",
            "handoff_billing",
            "handoff_engineering",
            "handoff_sre",
            "handoff_security",
            "handoff_privacy",
            "manual_reconciliation",
        }:
            return normalized
        # Agent nodes sometimes include their diagnostic summary in this
        # argument.  Treat any non-protocol terminal text as a failed run so
        # the ticket lease is released and the next webhook can recover it.
        return "failed"
