from __future__ import annotations

import base64
import json
import copy
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse
from uuid import uuid4

from .client import ZendeskApiError, ZendeskClient
from .mcp_schema import PRIORITY_VALUES, STATUS_VALUES, TYPE_VALUES
from .settings import Settings, get_settings


FORBIDDEN_COMMENT_KEYS = {
    "comment",
    "public",
    "body",
    "html_body",
    "plain_body",
    "comment_body",
    "uploads",
    "upload",
    "attachment",
    "attachments",
}
FORBIDDEN_INTERNAL_NOTE_KEYS = FORBIDDEN_COMMENT_KEYS - {"body"}

READ_ONLY_TOOL_NAMES = {
    "get_ticket",
    "list_tickets",
    "search_tickets",
    "get_ticket_comments",
    "get_ticket_conversation_log",
    "list_attachments",
    "download_attachment",
    "get_user",
    "list_views",
    "get_view_tickets",
    "list_groups",
    "search_help_center",
    "get_article",
}

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_CACHE_LOAD_LOCKS: dict[tuple[str, str], threading.Lock] = {}


def handle_tool_call(name: str, arguments: dict[str, Any], *, settings: Optional[Settings] = None) -> dict[str, Any]:
    settings = settings or get_settings()
    handlers: dict[str, Callable[[dict[str, Any], Settings], dict[str, Any]]] = {
        "get_ticket": _get_ticket,
        "list_tickets": _list_tickets,
        "search_tickets": _search_tickets,
        "get_ticket_comments": _get_ticket_comments,
        "get_ticket_conversation_log": _get_ticket_conversation_log,
        "list_attachments": _list_attachments,
        "download_attachment": _download_attachment,
        "get_user": _get_user,
        "list_views": _list_views,
        "get_view_tickets": _get_view_tickets,
        "list_groups": _list_groups,
        "search_help_center": _search_help_center,
        "get_article": _get_article,
        "add_internal_note": _add_internal_note,
        "update_ticket_status": _update_ticket_status,
        "update_ticket_metadata": _update_ticket_metadata,
    }
    handler = handlers.get(name)
    if not handler:
        return _failure("UNKNOWN_TOOL", f"unknown tool: {name}")
    try:
        return handler(arguments or {}, settings)
    except ZendeskApiError as e:
        if name in READ_ONLY_TOOL_NAMES and e.status_code == 0:
            time.sleep(0.25)
            try:
                return handler(arguments or {}, settings)
            except ZendeskApiError as retry_error:
                e = retry_error
        return _failure(
            "ZENDESK_API_ERROR",
            e.message,
            status_code=e.status_code,
            detail=e.detail,
        )
    except ValueError as e:
        return _failure("BAD_ARGUMENTS", str(e))


def _get_ticket(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    ticket_id = _required_int(args, "ticket_id")
    client = ZendeskClient(settings)
    data = client.get_ticket(ticket_id)
    comments: list[dict[str, Any]] = []
    conversation_events: list[dict[str, Any]] = []
    if args.get("include_comments"):
        comments = client.get_ticket_comments(ticket_id).get("comments", [])
        data["comments"] = comments
    if args.get("include_conversation_log"):
        updated_at = (data.get("ticket") or {}).get("updated_at")
        data["conversation_log"] = _cached_conversation_log(
            ticket_id,
            args,
            settings,
            cache_version=str(updated_at) if updated_at else None,
        )
        conversation_events = (
            data["conversation_log"].get("events")
            or data["conversation_log"].get("conversation_log", {}).get("events")
            or []
        )
    if not args.get("include_attachment_files"):
        return _success(data=data)

    if not comments:
        comments = client.get_ticket_comments(ticket_id).get("comments", [])
    if not conversation_events:
        updated_at = (data.get("ticket") or {}).get("updated_at")
        conversation = _cached_conversation_log(
            ticket_id,
            args,
            settings,
            cache_version=str(updated_at) if updated_at else None,
        )
        conversation_events = conversation.get("events") or conversation.get("conversation_log", {}).get("events") or []

    attachments = _dedupe_attachments(
        [*_attachments_from_comments(comments), *_attachments_from_conversation_events(conversation_events)]
    )
    max_files = _bounded_int(args.get("max_attachment_files", 8), "max_attachment_files", minimum=1, maximum=20)
    attachment_files: list[dict[str, Any]] = []
    attachment_file_errors: list[dict[str, str]] = []
    for attachment in attachments[:max_files]:
        content_url = str(attachment.get("content_url") or "").strip()
        if not content_url:
            continue
        downloaded = _download_attachment(
            {
                "content_url": content_url,
                "file_name": attachment.get("file_name"),
            },
            settings,
        )
        if downloaded.get("success") and isinstance(downloaded.get("data"), dict):
            attachment_files.append(downloaded["data"])
        else:
            attachment_file_errors.append(
                {
                    "file_name": str(attachment.get("file_name") or "attachment"),
                    "error": str(downloaded.get("error") or downloaded.get("code") or "download failed"),
                }
            )
    data["attachment_file_count"] = len(attachment_files)
    data["attachment_file_total"] = len(attachments)
    if attachment_file_errors:
        data["attachment_file_errors"] = attachment_file_errors
    return _success(data=data, _attachment_files=attachment_files)


def _list_tickets(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    data = ZendeskClient(settings).list_tickets(
        status=args.get("status"),
        assignee_id=_optional_int(args, "assignee_id"),
        page=_page(args),
        per_page=_per_page(args),
        sort_by=str(args.get("sort_by") or "updated_at"),
        sort_order=str(args.get("sort_order") or "desc"),
    )
    return _success(data=data)


def _search_tickets(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    query = _required_str(args, "query")
    data = ZendeskClient(settings).search_tickets(
        query=query,
        page=_page(args),
        per_page=_per_page(args),
        sort_by=str(args.get("sort_by") or "updated_at"),
        sort_order=str(args.get("sort_order") or "desc"),
    )
    return _success(data=data)


def _get_ticket_comments(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    ticket_id = _required_int(args, "ticket_id")
    client = ZendeskClient(settings)
    data = client.get_ticket_comments(ticket_id, page=_page(args), per_page=_per_page(args, default=100))
    if args.get("include_conversation_log"):
        data["conversation_log"] = _cached_conversation_log(ticket_id, args, settings)
    return _success(data=data)


def _get_ticket_conversation_log(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    ticket_id = _required_int(args, "ticket_id")
    return _success(data=_cached_conversation_log(ticket_id, args, settings))


def _list_attachments(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    ticket_id = _required_int(args, "ticket_id")
    client = ZendeskClient(settings)
    warnings: list[str] = []
    attachments: list[dict[str, Any]] = []
    after = _optional_str(args, "conversation_after") or _optional_str(args, "after")
    if after is None:
        comments = client.get_ticket_comments(ticket_id).get("comments", [])
        attachments.extend(_attachments_from_comments(comments))
    include_conversation_log = bool(args.get("include_conversation_log", True))
    conversation_meta: dict[str, Any] = {"has_more": False, "after_cursor": None}
    if include_conversation_log:
        try:
            conversation = _cached_conversation_log(ticket_id, args, settings)
            events = conversation.get("events") or conversation.get("conversation_log", {}).get("events") or []
            attachments.extend(_attachments_from_conversation_events(events))
            raw_meta = conversation.get("meta") or conversation.get("conversation_log", {}).get("meta") or {}
            conversation_meta = {
                "has_more": bool(raw_meta.get("has_more")),
                "after_cursor": raw_meta.get("after_cursor"),
            }
        except ZendeskApiError as e:
            warnings.append(f"conversation_log skipped: {e.message}")
    return _success(
        data={
            "ticket_id": ticket_id,
            "attachments": _dedupe_attachments(attachments),
            "conversation_log_included": include_conversation_log,
            "meta": conversation_meta,
            "warnings": warnings,
        }
    )


def _download_attachment(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    content_url = _required_str(args, "content_url")
    if not _is_allowed_attachment_url(content_url, settings):
        return _failure(
            "BAD_ATTACHMENT_URL",
            "download_attachment only accepts same-subdomain Zendesk attachment URLs",
        )
    requested_max = args.get("max_bytes", settings.download_max_bytes)
    max_bytes = _bounded_int(
        requested_max,
        "max_bytes",
        minimum=1,
        maximum=min(settings.download_max_bytes, 25 * 1024 * 1024),
    )
    file_name = _optional_str(args, "file_name") or _file_name_from_url(content_url)
    downloaded = ZendeskClient(settings).download_attachment(
        content_url,
        max_bytes=max_bytes,
    )
    content = downloaded.get("content") or b""
    if not isinstance(content, bytes):
        raise ValueError("downloaded content must be bytes")
    return _success(
        data={
            "content_url": content_url,
            "file_name": file_name,
            "content_type": downloaded.get("content_type") or "application/octet-stream",
            "size": downloaded.get("size", len(content)),
            "encoding": "base64",
            "data_base64": base64.b64encode(content).decode("ascii"),
        }
    )


def _get_user(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    return _success(data=ZendeskClient(settings).get_user(_required_int(args, "user_id")))


def _list_views(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    data = _cached_read("list_views", {}, settings, lambda: ZendeskClient(settings).list_views())
    return _success(data=data)


def _get_view_tickets(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    data = ZendeskClient(settings).get_view_tickets(
        _required_int(args, "view_id"),
        page=_page(args),
        per_page=_per_page(args),
    )
    return _success(data=data)


def _list_groups(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    data = _cached_read("list_groups", {}, settings, lambda: ZendeskClient(settings).list_groups())
    return _success(data=data)


def _search_help_center(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    cache_args = {
        "query": _required_str(args, "query"),
        "locale": str(args.get("locale") or "zh-cn"),
        "page": _page(args),
        "per_page": _per_page(args),
    }
    data = _cached_read(
        "search_help_center",
        cache_args,
        settings,
        lambda: ZendeskClient(settings).search_help_center(**cache_args),
    )
    return _success(data=data)


def _get_article(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    cache_args = {
        "article_id": _required_int(args, "article_id"),
        "locale": str(args.get("locale") or "zh-cn"),
    }
    data = _cached_read(
        "get_article",
        cache_args,
        settings,
        lambda: ZendeskClient(settings).get_article(cache_args["article_id"], locale=cache_args["locale"]),
    )
    return _success(data=data)


def _cached_conversation_log(
    ticket_id: int,
    args: dict[str, Any],
    settings: Settings,
    *,
    cache_version: Optional[str] = None,
) -> dict[str, Any]:
    page_size = _bounded_int(
        args.get("conversation_page_size", args.get("page_size", 100)),
        "conversation_page_size",
        minimum=1,
        maximum=100,
    )
    after = _optional_str(args, "conversation_after") or _optional_str(args, "after")
    force_refresh = bool(args.get("force_refresh", False))
    cache_version = cache_version or _optional_str(args, "ticket_updated_at")
    cache_version = cache_version or _optional_str(args, "conversation_cache_version")
    client: Optional[ZendeskClient] = None
    if cache_version is None:
        client = ZendeskClient(settings)
        ticket = client.get_ticket(ticket_id).get("ticket") or {}
        updated_at = ticket.get("updated_at")
        cache_version = str(updated_at) if updated_at else None

    cache_args = {
        "ticket_id": ticket_id,
        "conversation_page_size": page_size,
        "conversation_after": after,
        "ticket_updated_at": cache_version,
    }

    def load_conversation_log() -> dict[str, Any]:
        nonlocal client
        if client is None:
            client = ZendeskClient(settings)
        return client.get_ticket_conversation_log(
            ticket_id,
            page_size=page_size,
            after=after,
        )

    return _cached_read(
        "get_ticket_conversation_log",
        cache_args,
        settings,
        load_conversation_log,
        force_refresh=force_refresh,
    )


def _update_ticket_status(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    explicit = str(args.get("explicit_user_status_request") or "").strip()
    if not explicit:
        return _failure(
            "EXPLICIT_STATUS_REQUEST_REQUIRED",
            "update_ticket_status requires explicit_user_status_request copied from the user's status-change request",
        )
    ticket_id = _required_int(args, "ticket_id")
    status = _required_str(args, "status")
    if status not in STATUS_VALUES:
        return _failure("BAD_STATUS", f"status must be one of: {', '.join(STATUS_VALUES)}")
    if _contains_forbidden_comment_key(args):
        return _failure("COMMENT_FIELDS_FORBIDDEN", "status updates cannot include comments or attachments")

    dry_run = bool(args.get("dry_run", True))
    confirm = bool(args.get("confirm", False))
    if not dry_run and not confirm:
        return _failure("CONFIRM_REQUIRED", "set confirm=true to execute a non-dry-run status update")
    if status == "solved" and not dry_run and not bool(args.get("confirm_solve", False)):
        return _failure("CONFIRM_SOLVE_REQUIRED", "solved status requires confirm_solve=true")

    client = ZendeskClient(settings)
    current = client.get_ticket(ticket_id).get("ticket", {})
    stale = _stale_snapshot(args, current)
    if stale:
        return stale
    before_status = current.get("status")
    diff = {"status": {"before": before_status, "after": status}}
    payload = {
        "action": "update_ticket_status",
        "ticket_id": ticket_id,
        "dry_run": dry_run,
        "actor": str(args.get("actor") or "unknown"),
        "explicit_user_request": explicit,
        "diff": diff,
        "before": _ticket_snapshot(current),
    }
    if dry_run or before_status == status:
        payload["after"] = {"status": status, "updated_at": current.get("updated_at")}
        payload["skipped"] = before_status == status
        return _success(**payload)

    updated = client.update_ticket(ticket_id, {"status": status}).get("ticket", {})
    payload["after"] = _ticket_snapshot(updated)
    payload["audit_id"] = _write_audit(settings, payload)
    return _success(**payload)


def _add_internal_note(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    explicit = str(args.get("explicit_user_internal_note_request") or "").strip()
    if not explicit:
        return _failure(
            "EXPLICIT_INTERNAL_NOTE_REQUEST_REQUIRED",
            "add_internal_note requires explicit_user_internal_note_request copied from the user's internal-note request",
        )
    if _contains_forbidden_internal_note_key(args):
        return _failure("PUBLIC_COMMENT_FORBIDDEN", "internal notes cannot include public comments or attachments")

    ticket_id = _required_int(args, "ticket_id")
    body = _required_str(args, "body")
    dry_run = bool(args.get("dry_run", True))
    confirm = bool(args.get("confirm", False))
    if not dry_run and not confirm:
        return _failure("CONFIRM_REQUIRED", "set confirm=true to execute a non-dry-run internal note")

    client = ZendeskClient(settings)
    current = client.get_ticket(ticket_id).get("ticket", {})
    stale = _stale_snapshot(args, current)
    if stale:
        return stale

    payload = {
        "action": "add_internal_note",
        "ticket_id": ticket_id,
        "dry_run": dry_run,
        "actor": str(args.get("actor") or "unknown"),
        "explicit_user_request": explicit,
        "body_preview": _text_preview(body),
        "before": _ticket_snapshot(current),
    }
    if dry_run:
        payload["after"] = {"preview": True}
        return _success(**payload)

    updated = client.add_internal_note(ticket_id, body).get("ticket", {})
    payload["after"] = _ticket_snapshot(updated)
    payload["audit_id"] = _write_audit(settings, payload)
    return _success(**payload)


def _update_ticket_metadata(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    if _contains_forbidden_comment_key(args):
        return _failure("COMMENT_FIELDS_FORBIDDEN", "metadata updates cannot include comments or attachments")
    explicit = str(args.get("explicit_user_metadata_request") or "").strip()
    if not explicit:
        return _failure(
            "EXPLICIT_METADATA_REQUEST_REQUIRED",
            "update_ticket_metadata requires explicit_user_metadata_request copied from the user's metadata-change request",
        )
    ticket_id = _required_int(args, "ticket_id")
    dry_run = bool(args.get("dry_run", True))
    confirm = bool(args.get("confirm", False))
    if not dry_run and not confirm:
        return _failure("CONFIRM_REQUIRED", "set confirm=true to execute a non-dry-run metadata update")

    fields = _metadata_fields(args, settings)
    requested_tags = _updated_tags({}, args)
    if not fields and requested_tags is None:
        return _failure("NO_METADATA_FIELDS", "no supported metadata fields were provided")

    client = ZendeskClient(settings)
    current = client.get_ticket(ticket_id).get("ticket", {})
    stale = _stale_snapshot(args, current)
    if stale:
        return stale

    diff = _metadata_diff(current, args)
    updated_tags = _updated_tags(current, args)
    if updated_tags is not None:
        fields["tags"] = updated_tags
    payload = {
        "action": "update_ticket_metadata",
        "ticket_id": ticket_id,
        "dry_run": dry_run,
        "actor": str(args.get("actor") or "unknown"),
        "explicit_user_request": explicit,
        "diff": diff,
        "before": _ticket_snapshot(current),
        "fields": fields,
    }
    if dry_run:
        payload["after"] = {"preview": True}
        return _success(**payload)

    update_fields = {
        **fields,
        "safe_update": True,
        "updated_stamp": current.get("updated_at"),
    }
    updated = client.update_ticket(ticket_id, update_fields).get("ticket", {})
    payload["after"] = _ticket_snapshot(updated)
    payload["audit_id"] = _write_audit(settings, payload)
    return _success(**payload)


def _metadata_fields(args: dict[str, Any], settings: Settings) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "priority" in args and args.get("priority") is not None:
        priority = str(args["priority"])
        if priority not in PRIORITY_VALUES:
            raise ValueError(f"priority must be one of: {', '.join(PRIORITY_VALUES)}")
        fields["priority"] = priority
    if "type" in args and args.get("type") is not None:
        ticket_type = str(args["type"])
        if ticket_type not in TYPE_VALUES:
            raise ValueError(f"type must be one of: {', '.join(TYPE_VALUES)}")
        fields["type"] = ticket_type
    if args.get("assignee_id") is not None:
        fields["assignee_id"] = _required_int(args, "assignee_id")
    if args.get("group_id") is not None:
        fields["group_id"] = _required_int(args, "group_id")
    custom_fields = args.get("custom_fields") or []
    if custom_fields:
        fields["custom_fields"] = _validate_custom_fields(custom_fields, settings)
    return fields


def _validate_custom_fields(value: Any, settings: Settings) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("custom_fields must be an array")
    allowed = settings.custom_field_id_set
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("custom_fields items must be objects")
        field_id = str(item.get("id") or "").strip()
        if not field_id:
            raise ValueError("custom field id is required")
        if allowed and field_id not in allowed:
            raise ValueError(f"custom field {field_id} is not in ZENDESK_MCP_ALLOWED_CUSTOM_FIELD_IDS")
        result.append({"id": int(field_id) if field_id.isdigit() else field_id, "value": item.get("value")})
    return result


def _metadata_diff(current: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    diff: dict[str, Any] = {}
    for key in ("priority", "type", "assignee_id", "group_id"):
        if key in args and args.get(key) is not None:
            diff[key] = {"before": current.get(key), "after": args.get(key)}
    updated_tags = _updated_tags(current, args)
    if updated_tags is not None:
        diff["tags"] = {"before": sorted(set(current.get("tags") or [])), "after": updated_tags}
    if args.get("custom_fields"):
        diff["custom_fields"] = {"before": "not expanded", "after": args.get("custom_fields")}
    return diff


def _updated_tags(current: dict[str, Any], args: dict[str, Any]) -> Optional[list[str]]:
    add_tags = _clean_str_list(args.get("add_tags"))
    remove_tags = _clean_str_list(args.get("remove_tags"))
    if not add_tags and not remove_tags:
        return None
    current_tags = set(current.get("tags") or [])
    return sorted((current_tags | set(add_tags)) - set(remove_tags))


def _cached_read(
    name: str,
    args: dict[str, Any],
    settings: Settings,
    loader: Callable[[], dict[str, Any]],
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    ttl = settings.cache_ttl_seconds
    max_entries = settings.cache_max_entries
    if ttl <= 0 or max_entries <= 0:
        return loader()

    key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if not force_refresh and cached and cached[0] > now:
            return copy.deepcopy(cached[1])
        load_lock = _CACHE_LOAD_LOCKS.setdefault(key, threading.Lock())

    with load_lock:
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if not force_refresh and cached and cached[0] > now:
                return copy.deepcopy(cached[1])

        try:
            data = loader()
        except ZendeskApiError as e:
            if not force_refresh:
                with _CACHE_LOCK:
                    cached = _CACHE.get(key)
                if cached:
                    stale = copy.deepcopy(cached[1])
                    if isinstance(stale, dict):
                        stale["_cache"] = {
                            "stale": True,
                            "error": e.message,
                            "detail": e.detail,
                        }
                    return stale
            raise

        with _CACHE_LOCK:
            now = time.monotonic()
            _prune_cache_locked(now, max_entries)
            _CACHE[key] = (now + ttl, copy.deepcopy(data))
        return data


def _prune_cache_locked(now: float, max_entries: int) -> None:
    for key, (expires_at, _) in list(_CACHE.items()):
        if expires_at <= now:
            _CACHE.pop(key, None)
    while len(_CACHE) >= max_entries:
        oldest_key = min(_CACHE, key=lambda key: _CACHE[key][0])
        _CACHE.pop(oldest_key, None)


def _stale_snapshot(args: dict[str, Any], current: dict[str, Any]) -> Optional[dict[str, Any]]:
    expected_updated_at = args.get("expected_updated_at")
    if expected_updated_at and expected_updated_at != current.get("updated_at"):
        return _failure(
            "STALE_TICKET_SNAPSHOT",
            "ticket updated_at changed; refresh the ticket before updating status or metadata",
            expected_updated_at=expected_updated_at,
            actual_updated_at=current.get("updated_at"),
        )
    return None


def _attachments_from_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for comment in comments:
        for attachment in comment.get("attachments") or []:
            rows.append(
                {
                    "source": "comment",
                    "comment_id": comment.get("id"),
                    "author_id": comment.get("author_id"),
                    "created_at": comment.get("created_at"),
                    "id": attachment.get("id"),
                    "file_name": attachment.get("file_name") or attachment.get("filename"),
                    "content_url": attachment.get("content_url"),
                    "content_type": attachment.get("content_type"),
                    "size": attachment.get("size"),
                }
            )
    return rows


def _attachments_from_conversation_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        author = event.get("author") or {}
        for attachment in event.get("attachments") or []:
            rows.append(
                {
                    "source": "conversation_log",
                    "event_id": event.get("id"),
                    "author_id": author.get("id") or author.get("zen:support:user_id"),
                    "created_at": event.get("created_at") or event.get("timestamp"),
                    "id": attachment.get("id"),
                    "file_name": attachment.get("file_name") or attachment.get("filename") or attachment.get("name"),
                    "content_url": attachment.get("content_url") or attachment.get("url"),
                    "content_type": attachment.get("content_type") or attachment.get("mime_type"),
                    "size": attachment.get("size"),
                }
            )
        content = event.get("content") or {}
        media_url = content.get("media_url") if isinstance(content, dict) else None
        if media_url:
            rows.append(
                {
                    "source": "conversation_log",
                    "event_id": event.get("id"),
                    "author_id": author.get("id") or author.get("zen:support:user_id"),
                    "created_at": event.get("created_at") or event.get("timestamp"),
                    "id": content.get("attachment_id"),
                    "file_name": content.get("alt_text") or _file_name_from_url(str(media_url)),
                    "content_url": media_url,
                    "content_type": content.get("media_type"),
                    "size": content.get("media_size"),
                }
            )
    return rows


def _dedupe_attachments(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attachment in attachments:
        content_url = str(attachment.get("content_url") or "").strip()
        key = content_url or str(attachment.get("id") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(attachment)
    return result


def _is_allowed_attachment_url(content_url: str, settings: Settings) -> bool:
    parsed = urlparse(content_url)
    if parsed.scheme != "https":
        return False
    expected_host = f"{settings.zendesk_subdomain}.zendesk.com"
    if parsed.hostname != expected_host:
        return False
    path = parsed.path or ""
    return any(
        marker in path
        for marker in (
            "/attachments/",
            "/ticket_attachments/",
            "/sc/attachments/",
        )
    )


def _file_name_from_url(content_url: str) -> str:
    parsed = urlparse(content_url)
    query_name = ""
    for part in (parsed.query or "").split("&"):
        key, sep, value = part.partition("=")
        if sep and key == "name" and value:
            query_name = unquote(value)
            break
    if query_name:
        return query_name.rsplit("/", 1)[-1] or "attachment"
    path_name = unquote((parsed.path or "").rstrip("/").rsplit("/", 1)[-1])
    return path_name or "attachment"


def _contains_forbidden_comment_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key) in FORBIDDEN_COMMENT_KEYS:
                return True
            if _contains_forbidden_comment_key(nested):
                return True
    if isinstance(value, list):
        return any(_contains_forbidden_comment_key(item) for item in value)
    return False


def _contains_forbidden_internal_note_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key) in FORBIDDEN_INTERNAL_NOTE_KEYS:
                return True
            if _contains_forbidden_internal_note_key(nested):
                return True
    if isinstance(value, list):
        return any(_contains_forbidden_internal_note_key(item) for item in value)
    return False


def _text_preview(text: str) -> str:
    if len(text) <= 500:
        return text
    return f"{text[:497]}..."


def _write_audit(settings: Settings, record: dict[str, Any]) -> str:
    audit_id = str(uuid4())
    path = Path(settings.audit_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "audit_id": audit_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **record,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    return audit_id


def _ticket_snapshot(ticket: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "id",
        "status",
        "priority",
        "type",
        "assignee_id",
        "group_id",
        "tags",
        "updated_at",
    ]
    return {key: ticket.get(key) for key in keys if key in ticket}


def _success(**payload: Any) -> dict[str, Any]:
    return {"success": True, **payload}


def _failure(code: str, error: str, **payload: Any) -> dict[str, Any]:
    return {"success": False, "code": code, "error": error, **payload}


def _required_int(args: dict[str, Any], name: str) -> int:
    value = args.get(name)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


def _optional_int(args: dict[str, Any], name: str) -> Optional[int]:
    if args.get(name) is None:
        return None
    return _required_int(args, name)


def _required_str(args: dict[str, Any], name: str) -> str:
    value = str(args.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _optional_str(args: dict[str, Any], name: str) -> Optional[str]:
    if args.get(name) is None:
        return None
    value = str(args.get(name) or "").strip()
    return value or None


def _clean_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("tag fields must be arrays")
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _page(args: dict[str, Any]) -> int:
    return _bounded_int(args.get("page", 1), "page", minimum=1, maximum=10_000)


def _per_page(args: dict[str, Any], *, default: int = 25) -> int:
    return _bounded_int(args.get("per_page", default), "per_page", minimum=1, maximum=100)


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed
