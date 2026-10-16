# Zendesk MCP

Remote MCP service for guarded Zendesk support-ticket access.

## Scope

Included:

- `get_ticket`
- `list_tickets`
- `search_tickets`
- `get_ticket_comments`
- `list_attachments`
- `download_attachment`
- `get_user`
- `list_views`
- `get_view_tickets`
- `list_groups`
- `search_help_center`
- `get_article`
- `add_internal_note`
- `update_ticket_status`
- `update_ticket_metadata`

Explicitly excluded:

- public replies
- public comments
- solve-with-reply or any comment-bearing update

`add_internal_note` only adds private Zendesk internal notes (`public=false`). It only runs when `explicit_user_internal_note_request` is provided. A real non-dry-run write also requires `confirm=true`. It rejects public-comment and attachment-like payload fields.

`update_ticket_status` only runs when `explicit_user_status_request` is provided. A real non-dry-run write also requires `confirm=true`. `status=solved` additionally requires `confirm_solve=true`.

`update_ticket_metadata` only changes non-comment metadata. It supports priority, type, assignee, group, incremental tag add/remove, and optional custom fields. It never accepts `comment`, `body`, `public`, or attachment-like fields.

## Ticket Read Latency

`get_ticket` and `get_ticket_comments` are lightweight at the tool level unless
`include_conversation_log=true` is explicitly set. For Dify SaaS support-ticket
work, the customer thread is Zendesk Messaging, so the support workflow should
include the conversation log once while gathering ticket context.

`list_attachments` includes Messaging conversation-log attachments by default.
Pass `include_conversation_log=false` only for a comments-only fast path.

`download_attachment` accepts only same-subdomain Zendesk attachment URLs such
as `https://<subdomain>.zendesk.com/attachments/...`, returns base64 content,
and is bounded by `ZENDESK_MCP_DOWNLOAD_MAX_BYTES` to avoid oversized transfers.

Use `get_ticket_conversation_log` when the investigation needs the Messaging
thread directly. Conversation-log reads are cached for
`ZENDESK_MCP_CACHE_TTL_SECONDS` and bounded by `ZENDESK_MCP_CACHE_MAX_ENTRIES`,
so repeated reads during the same investigation should avoid re-fetching the
same heavy Zendesk endpoint. The cache key includes the ticket `updated_at`
value, so a new customer reply invalidates the cached conversation log. Pass
`force_refresh=true` only when you explicitly need to bypass cache for a read.

Keep `ZENDESK_MCP_WORKERS=1` when relying on the in-memory conversation-log
cache. The app still runs blocking Zendesk calls in a threadpool, while one
process keeps cache hits consistent across sequential agent requests.

## Local

```bash
cd /Users/yangyonggang/appdata/zd-mcp/zendesk-mcp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
cp .env.example .env
zendesk-mcp
```

List tools:

```bash
curl -sS -X POST http://127.0.0.1:8100/mcp \
  -H "Authorization: Bearer $ZENDESK_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Dry-run a status change:

```bash
curl -sS -X POST http://127.0.0.1:8100/mcp \
  -H "Authorization: Bearer $ZENDESK_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"update_ticket_status","arguments":{"ticket_id":123,"status":"pending","explicit_user_status_request":"把 123 改成 pending","dry_run":true}}}'
```

Dry-run an internal note:

```bash
curl -sS -X POST http://127.0.0.1:8100/mcp \
  -H "Authorization: Bearer $ZENDESK_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add_internal_note","arguments":{"ticket_id":123,"body":"Investigated from logs.","explicit_user_internal_note_request":"给 123 加内部备注","dry_run":true}}}'
```

## Server Deploy

```bash
sudo mkdir -p /opt/zendesk-mcp/code /opt/zendesk-mcp/logs /etc/zendesk-mcp
sudo rsync -a --delete ./ /opt/zendesk-mcp/code/
cd /opt/zendesk-mcp/code
python3 -m venv .venv
sudo .venv/bin/pip install 'fastapi>=0.115' 'requests>=2.31' 'uvicorn[standard]>=0.30'
sudo cp .env.example /etc/zendesk-mcp/env
sudo chmod 600 /etc/zendesk-mcp/env
sudo cp deploy/systemd/zendesk-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zendesk-mcp
```

Nginx location:

```nginx
location /zendesk-mcp/ {
    proxy_pass http://127.0.0.1:8100/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
}
```

Public MCP endpoint:

```text
https://zendesk.smlershou.top/zendesk-mcp/mcp
```
