from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .coordination import CodexResultAcceptance
from .schemas import CodexCompletionRequest


class ResultDispatchError(RuntimeError):
    pass


class ContinuationDispatchError(RuntimeError):
    pass


class CodexContinuationDispatcher:
    """Create/reuse the next Runner job for a dirty Ticket Actor cursor."""

    def __init__(
        self,
        *,
        runner_url: str,
        token: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        parsed = urlparse(runner_url)
        is_loopback_http = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if parsed.scheme != "https" and not is_loopback_http:
            raise ValueError("Runner URL must use HTTPS or loopback HTTP")
        if len(token) < 16:
            raise ValueError("Runner token is too short")
        self.runner_url = runner_url
        self.token = token
        self.timeout_seconds = timeout_seconds

    async def start(self, ticket_id: int, request_id: str) -> str:
        body = json.dumps(
            {"ticket_id": ticket_id, "request_id": request_id},
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            self.runner_url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        def execute_request() -> tuple[int, bytes]:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return int(response.status), response.read(1024 * 1024 + 1)

        try:
            status_code, raw = await asyncio.to_thread(execute_request)
        except OSError as exc:
            raise ContinuationDispatchError("Runner continuation request failed") from exc
        if status_code not in {200, 202}:
            raise ContinuationDispatchError(
                f"Runner continuation returned HTTP {status_code}"
            )
        if len(raw) > 1024 * 1024:
            raise ContinuationDispatchError("Runner continuation response is too large")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ContinuationDispatchError(
                "Runner continuation returned invalid JSON"
            ) from exc
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
            job_id,
        ):
            raise ContinuationDispatchError("Runner continuation returned invalid job ID")
        return job_id


class DifyResultDispatcher:
    """Invoke the short Dify result stage after a fenced Codex completion."""

    def __init__(
        self,
        *,
        workflow_url: str,
        api_key: str,
        user: str = "zendesk-ticket-actor",
        timeout_seconds: float = 60.0,
    ) -> None:
        parsed = urlparse(workflow_url)
        is_loopback_http = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if parsed.scheme != "https" and not is_loopback_http:
            raise ValueError("Dify result workflow URL must use HTTPS or loopback HTTP")
        if len(api_key) < 16:
            raise ValueError("Dify result workflow API key is too short")
        self.workflow_url = workflow_url
        self.api_key = api_key
        self.user = user
        self.timeout_seconds = timeout_seconds

    async def dispatch(
        self,
        payload: CodexCompletionRequest,
        acceptance: CodexResultAcceptance,
    ) -> None:
        customer_message_count = acceptance.customer_message_count
        if payload.data is not None:
            customer_message_count = payload.data[
                "analyzed_customer_message_count"
            ]
        body: dict[str, Any] = {
            "inputs": {
                "stage": "codex_result",
                "ticket_id": str(payload.ticket_id),
                "run_token": acceptance.run_token,
                "job_id": payload.job_id,
                "customer_last_message_id": acceptance.customer_last_message_id,
                "customer_message_count": str(customer_message_count),
                "codex_status": payload.status,
                "codex_error": payload.error or "none",
                "codex_result_json": json.dumps(
                    payload.data or {}, ensure_ascii=False, separators=(",", ":")
                ),
            },
            "response_mode": "blocking",
            "user": self.user,
        }
        request = Request(
            self.workflow_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Dify-Zendesk-Relay/1.0",
            },
            method="POST",
        )

        def execute_request() -> tuple[int, bytes]:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return int(response.status), response.read(1024 * 1024 + 1)

        try:
            status_code, raw = await asyncio.to_thread(execute_request)
        except OSError as exc:
            raise ResultDispatchError("Dify result workflow request failed") from exc
        if status_code < 200 or status_code >= 300:
            raise ResultDispatchError(
                f"Dify result workflow returned HTTP {status_code}"
            )
        if len(raw) > 1024 * 1024:
            raise ResultDispatchError("Dify result workflow response is too large")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ResultDispatchError("Dify result workflow returned invalid JSON") from exc
        if result.get("data", {}).get("status") != "succeeded":
            raise ResultDispatchError("Dify result workflow did not succeed")
