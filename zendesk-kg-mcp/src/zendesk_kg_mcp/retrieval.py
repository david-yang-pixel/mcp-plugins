import asyncio
import json
from typing import Any

import httpx

from .models import RetrievalRequest
from .settings import Settings

ENDPOINT = "https://de-zendesk-kg.langgenius.app/v1/retrieval"


class RetrievalError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


class RetrievalClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings, self.transport = settings, transport

    async def search(self, request: RetrievalRequest) -> dict[str, Any]:
        key = self.settings.upstream_api_key.get_secret_value()
        if not key:
            raise RetrievalError("NOT_CONFIGURED", "The knowledge-base API key is not configured.")
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=self.settings.timeout_seconds, follow_redirects=False,
                    trust_env=False, transport=self.transport,
                ) as client:
                    async with client.stream(
                        "POST", ENDPOINT,
                        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
                        json=request.model_dump(mode="json", exclude_none=True),
                    ) as response:
                        status = response.status_code
                        if status in (401, 403):
                            raise RetrievalError("UPSTREAM_AUTH_FAILED", "The knowledge-base API rejected its API key.", status)
                        if status == 400:
                            raise RetrievalError("UPSTREAM_BAD_REQUEST", "The knowledge-base API rejected the search parameters.", status)
                        if status == 429:
                            raise RetrievalError("UPSTREAM_RATE_LIMITED", "The knowledge-base API is rate limited; retry later.", status)
                        if not 200 <= status < 300:
                            raise RetrievalError("UPSTREAM_HTTP_ERROR", "The knowledge-base API returned an unsuccessful response.", status)
                        if response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                            raise RetrievalError("UPSTREAM_INVALID_RESPONSE", "Expected a JSON knowledge-base response.")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > self.settings.max_response_bytes:
                                raise RetrievalError("RESPONSE_TOO_LARGE", "Response exceeded the size limit; reduce topK or optional include blocks.")
        except (TimeoutError, httpx.TimeoutException):
            raise RetrievalError("UPSTREAM_TIMEOUT", "The knowledge-base search timed out; retry later.") from None
        except httpx.RequestError:
            raise RetrievalError("UPSTREAM_UNAVAILABLE", "Could not connect to the knowledge-base API.") from None
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise RetrievalError("UPSTREAM_INVALID_RESPONSE", "The knowledge-base API returned invalid JSON.") from None
        if (not isinstance(result, dict) or not isinstance(result.get("results"), list)
                or not all(isinstance(item, dict) for item in result["results"])
                or not isinstance(result.get("searchId"), str) or not result["searchId"]):
            raise RetrievalError("UPSTREAM_INVALID_RESPONSE", "The knowledge-base response is missing results or searchId.")
        # Preserve summaries, links, confidence, searchId and optional blocks without reinterpretation.
        return result
