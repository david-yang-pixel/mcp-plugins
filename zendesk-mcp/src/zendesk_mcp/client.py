from __future__ import annotations

import threading
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from requests.exceptions import ConnectionError, Timeout

from .settings import Settings, get_settings


class ZendeskApiError(Exception):
    def __init__(self, status_code: int, message: str, detail: str = "") -> None:
        self.status_code = status_code
        self.message = message
        self.detail = detail
        super().__init__(f"[{status_code}] {message}")


_THREAD_LOCAL = threading.local()


class ZendeskClient:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.zendesk_base_url
        self.session = _thread_session(self.settings)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.settings.zendesk_subdomain:
            raise ZendeskApiError(503, "ZENDESK_SUBDOMAIN is not configured")
        if not self.settings.zendesk_email:
            raise ZendeskApiError(503, "ZENDESK_EMAIL is not configured")
        if not self.settings.zendesk_api_token:
            raise ZendeskApiError(503, "ZENDESK_API_TOKEN is not configured")

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_data,
                    timeout=(
                        self.settings.connect_timeout_seconds,
                        self.settings.read_timeout_seconds,
                    ),
                )
                break
            except Timeout:
                if attempt >= self.settings.max_retries:
                    raise ZendeskApiError(0, "Zendesk request timed out", endpoint)
            except ConnectionError:
                if attempt >= self.settings.max_retries:
                    raise ZendeskApiError(0, "Zendesk connection failed", endpoint)

        if response.status_code == 401:
            raise ZendeskApiError(401, "Zendesk authentication failed")
        if response.status_code == 403:
            raise ZendeskApiError(403, "Zendesk permission denied")
        if response.status_code == 404:
            raise ZendeskApiError(404, "Zendesk resource not found", endpoint)
        if response.status_code == 422:
            raise ZendeskApiError(422, "Zendesk validation failed", _response_detail(response))
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "60")
            raise ZendeskApiError(429, "Zendesk rate limit exceeded", f"retry_after={retry_after}")
        if not response.ok:
            raise ZendeskApiError(response.status_code, "Zendesk API request failed", _response_detail(response))
        if not response.content:
            return {}
        return response.json()

    def _ensure_configured(self) -> None:
        if not self.settings.zendesk_subdomain:
            raise ZendeskApiError(503, "ZENDESK_SUBDOMAIN is not configured")
        if not self.settings.zendesk_email:
            raise ZendeskApiError(503, "ZENDESK_EMAIL is not configured")
        if not self.settings.zendesk_api_token:
            raise ZendeskApiError(503, "ZENDESK_API_TOKEN is not configured")

    def _get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self._request("GET", endpoint, params=params)

    def _put(self, endpoint: str, json_data: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", endpoint, json_data=json_data)

    def get_ticket(self, ticket_id: int) -> dict[str, Any]:
        return self._get(f"tickets/{ticket_id}")

    def list_tickets(
        self,
        *,
        status: Optional[str] = None,
        assignee_id: Optional[int] = None,
        page: int = 1,
        per_page: int = 25,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        if status:
            return self.search_tickets(
                query=f"type:ticket status:{status}",
                page=page,
                per_page=per_page,
                sort_by=sort_by,
                sort_order=sort_order,
            )
        params = {
            "page": page,
            "per_page": per_page,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        if assignee_id:
            return self._get(f"users/{assignee_id}/tickets/assigned", params=params)
        return self._get("tickets", params=params)

    def search_tickets(
        self,
        *,
        query: str,
        page: int = 1,
        per_page: int = 25,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        normalized = query if "type:" in query else f"type:ticket {query}"
        return self._get(
            "search",
            params={
                "query": normalized,
                "page": page,
                "per_page": per_page,
                "sort_by": sort_by,
                "sort_order": sort_order,
            },
        )

    def update_ticket(self, ticket_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        return self._put(f"tickets/{ticket_id}", {"ticket": fields})

    def add_internal_note(self, ticket_id: int, body: str) -> dict[str, Any]:
        return self.update_ticket(
            ticket_id,
            {
                "comment": {
                    "body": body,
                    "public": False,
                }
            },
        )

    def get_ticket_comments(self, ticket_id: int, *, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        return self._get(
            f"tickets/{ticket_id}/comments",
            params={"page": page, "per_page": per_page},
        )

    def get_ticket_conversation_log(
        self,
        ticket_id: int,
        *,
        page_size: int = 100,
        after: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page[size]": page_size}
        if after:
            params["page[after]"] = after
        return self._get(f"tickets/{ticket_id}/conversation_log", params=params)

    def get_user(self, user_id: int) -> dict[str, Any]:
        return self._get(f"users/{user_id}")

    def list_views(self) -> dict[str, Any]:
        return self._get("views")

    def get_view_tickets(self, view_id: int, *, page: int = 1, per_page: int = 25) -> dict[str, Any]:
        return self._get(
            f"views/{view_id}/tickets",
            params={"page": page, "per_page": per_page},
        )

    def list_groups(self) -> dict[str, Any]:
        return self._get("groups")

    def search_help_center(
        self,
        *,
        query: str,
        locale: str = "zh-cn",
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        return self._get(
            "help_center/articles/search",
            params={
                "query": query,
                "locale": locale,
                "page": page,
                "per_page": per_page,
            },
        )

    def get_article(self, article_id: int, *, locale: str = "zh-cn") -> dict[str, Any]:
        return self._get(f"help_center/{locale}/articles/{article_id}")

    def download_attachment(self, content_url: str, *, max_bytes: int) -> dict[str, Any]:
        self._ensure_configured()
        sc_attachment_id = _sc_attachment_id(content_url)
        if sc_attachment_id:
            response = self._download_sc_attachment(sc_attachment_id, content_url)
        else:
            response = self._download_attachment_url(content_url)

        return _read_attachment_response(response, content_url, max_bytes)

    def _download_attachment_url(self, content_url: str) -> requests.Response:
        for attempt in range(self.settings.max_retries + 1):
            try:
                return self.session.get(
                    content_url,
                    # Attachments (including CDN redirects) are not JSON API responses.
                    headers={"Accept": "*/*"},
                    stream=True,
                    timeout=(
                        self.settings.connect_timeout_seconds,
                        self.settings.read_timeout_seconds,
                    ),
                )
            except Timeout:
                if attempt >= self.settings.max_retries:
                    raise ZendeskApiError(0, "Zendesk attachment download timed out", content_url)
            except ConnectionError:
                if attempt >= self.settings.max_retries:
                    raise ZendeskApiError(0, "Zendesk attachment download connection failed", content_url)
        raise ZendeskApiError(0, "Zendesk attachment download failed", content_url)

    def _download_sc_attachment(self, attachment_id: str, content_url: str) -> requests.Response:
        endpoint_url = f"{self.base_url}/ticket_attachments/{attachment_id}/content"
        for attempt in range(self.settings.max_retries + 1):
            try:
                response = self.session.get(
                    endpoint_url,
                    allow_redirects=False,
                    timeout=(
                        self.settings.connect_timeout_seconds,
                        self.settings.read_timeout_seconds,
                    ),
                )
                break
            except Timeout:
                if attempt >= self.settings.max_retries:
                    raise ZendeskApiError(0, "Zendesk attachment download timed out", content_url)
            except ConnectionError:
                if attempt >= self.settings.max_retries:
                    raise ZendeskApiError(0, "Zendesk attachment download connection failed", content_url)
        else:
            raise ZendeskApiError(0, "Zendesk attachment download failed", content_url)

        if response.status_code not in {301, 302, 303, 307, 308}:
            return response

        redirect_url = response.headers.get("Location")
        if not redirect_url:
            raise ZendeskApiError(502, "Zendesk attachment redirect missing Location", content_url)
        try:
            return requests.get(
                redirect_url,
                stream=True,
                timeout=(
                    self.settings.connect_timeout_seconds,
                    self.settings.read_timeout_seconds,
                ),
            )
        except Timeout:
            raise ZendeskApiError(0, "Zendesk attachment download timed out", content_url)
        except ConnectionError:
            raise ZendeskApiError(0, "Zendesk attachment download connection failed", content_url)


def _read_attachment_response(response: requests.Response, content_url: str, max_bytes: int) -> dict[str, Any]:
    if response.status_code == 401:
        raise ZendeskApiError(401, "Zendesk authentication failed")
    if response.status_code == 403:
        raise ZendeskApiError(403, "Zendesk permission denied")
    if response.status_code == 404:
        raise ZendeskApiError(404, "Zendesk resource not found", content_url)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "60")
        raise ZendeskApiError(429, "Zendesk rate limit exceeded", f"retry_after={retry_after}")
    if not response.ok:
        raise ZendeskApiError(
            response.status_code,
            "Zendesk attachment download failed",
            _response_detail(response),
        )

    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ZendeskApiError(413, "Zendesk attachment too large", f"max_bytes={max_bytes}")
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ZendeskApiError(413, "Zendesk attachment too large", f"max_bytes={max_bytes}")
        chunks.append(chunk)

    return {
        "content": b"".join(chunks),
        "content_type": (response.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip(),
        "size": total,
    }


def _sc_attachment_id(content_url: str) -> Optional[str]:
    parsed = urlparse(content_url)
    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) >= 4 and parts[:3] == ["sc", "attachments", "v2"]:
        return parts[3]
    return None


def _thread_session(settings: Settings) -> requests.Session:
    key = (
        settings.zendesk_base_url,
        settings.zendesk_email,
        settings.zendesk_api_token,
    )
    cached = getattr(_THREAD_LOCAL, "zendesk_session", None)
    if cached and cached[0] == key:
        return cached[1]

    session = requests.Session()
    session.auth = (
        f"{settings.zendesk_email}/token",
        settings.zendesk_api_token,
    )
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    _THREAD_LOCAL.zendesk_session = (key, session)
    return session


def _response_detail(response: requests.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text[:500]
    if isinstance(data, dict):
        detail = data.get("error") or data.get("description") or data.get("details")
        return str(detail)[:500]
    return str(data)[:500]
