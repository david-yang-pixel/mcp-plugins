# Zendesk Browser Relay

Independent HTTP relay for SaaS messaging tickets. It uses a persistent Chromium profile to send replies through Zendesk Agent Workspace when Conversations API credentials are unavailable.

## Security Boundary

- Stores Zendesk password and TOTP seed only in the server env file when automatic login is enabled.
- Never commits Zendesk password, TOTP seed, browser profile, or cookies to source control.
- Reuses a persistent Chromium profile and can regenerate TOTP codes when the browser session expires.
- API and MCP endpoints are protected by a Bearer token.
- If login is required, `/send` fails closed instead of falling back to Ticket Comments.
- Dify runs use a durable single-worker lease per ticket. Later webhook events
  are coalesced into that active run instead of starting a second agent.
- A worker must send with the newest customer snapshot. Any customer update
  observed while it was investigating invalidates an older draft.
- Immediately before the browser click, Relay refreshes the canonical customer
  cursor through the fixed server-configured Zendesk MCP endpoint. Dify input
  cannot select this destination or credential.
- A delivery with an unknown Browser Relay outcome remains fenced and is not
  automatically sent a second time.
- Explicit human requests and evidence-based specialist handoffs persist a
  `human_owned` lock. Later customer messages update the cursor and do not
  start another automated worker until an operator explicitly releases it.

## Setup

```bash
cd /Users/yangyonggang/appdata/zd-mcp/zendesk-browser-relay
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
cp .env.example .env
```

Set `.env`:

```bash
RELAY_API_KEY=replace-with-a-long-random-token
ZENDESK_SUBDOMAIN=langgenius
ZENDESK_PROFILE_DIR=/var/lib/zendesk-browser-relay/profile
TICKET_RUN_DB_PATH=/var/lib/zendesk-browser-relay/ticket-runs.sqlite3
TICKET_RUN_ACTIVE_LEASE_SECONDS=1800
TICKET_RUN_COALESCE_ACTIVE_MESSAGES=false
CODEX_COMPLETION_API_KEY=
DIFY_RESULT_WORKFLOW_URL=
DIFY_RESULT_WORKFLOW_API_KEY=
DIFY_RESULT_WORKFLOW_USER=zendesk-ticket-actor
CODEX_RUNNER_CREATE_URL=http://127.0.0.1:18087/v1/ticket-analyses
CODEX_RUNNER_API_KEY=
ZENDESK_MCP_URL=
ZENDESK_MCP_AUTHORIZATION=
ZENDESK_HEADLESS=true
ZENDESK_PROXY_SERVER=http://127.0.0.1:7890
ZENDESK_LOGIN_EMAIL=
ZENDESK_LOGIN_PASSWORD=
ZENDESK_TOTP_SECRET=
```

## Manual Login

On a machine with a display:

```bash
source .venv/bin/activate
zendesk-browser-login
```

Complete Zendesk login and 2FA in the browser, then press Enter in the terminal.

For a headless server, login locally with the same profile path structure, then sync the profile directory to the server.

## Server Install

```bash
sudo useradd --system --home /var/lib/zendesk-browser-relay --shell /usr/sbin/nologin zendesk-relay
sudo mkdir -p /opt/zendesk-browser-relay /var/lib/zendesk-browser-relay/profile /etc/zendesk-browser-relay
sudo chown -R zendesk-relay:zendesk-relay /var/lib/zendesk-browser-relay
sudo chmod 700 /var/lib/zendesk-browser-relay
```

Copy this project to `/opt/zendesk-browser-relay`, then:

```bash
cd /opt/zendesk-browser-relay
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium
sudo cp .env.example /etc/zendesk-browser-relay/env
sudo chmod 600 /etc/zendesk-browser-relay/env
sudo cp deploy/systemd/zendesk-browser-relay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zendesk-browser-relay
```

## Run

```bash
source .venv/bin/activate
uvicorn zendesk_browser_relay.app:app --host 127.0.0.1 --port 8097
```

## API

```bash
curl http://127.0.0.1:8097/health
```

```bash
curl -H "Authorization: Bearer $RELAY_API_KEY" \
  http://127.0.0.1:8097/session
```

```bash
curl -X POST http://127.0.0.1:8097/send \
  -H "Authorization: Bearer $RELAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"ticket_id":2861,"body":"Hello from relay"}'
```

## Remote MCP

The relay also exposes a minimal MCP JSON-RPC endpoint for remote agents:

```bash
curl -X POST https://zendesk.smlershou.top/zendesk-reply-mcp \
  -H "Authorization: Bearer $RELAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Tools:

```text
claim_zendesk_ticket_run
bind_codex_ticket_job
accept_codex_ticket_result
check_zendesk_ticket_run
finish_zendesk_ticket_run
send_zendesk_native_message
```

For a Dify customer-message workflow, use this order:

1. Call `claim_zendesk_ticket_run` immediately after validating the webhook.
   Pass the Dify workflow run ID as `worker_id`.
2. Continue only when `should_process` is `true`. With
   `TICKET_RUN_COALESCE_ACTIVE_MESSAGES=true`, a later webhook advances the
   desired cursor, marks the run dirty, and ends without starting another job.
   `human_owned` must also end immediately after notifying the human owner.
3. The short Dify start stage creates/reuses the asynchronous Runner job and ends
   without polling. Runner's authenticated callback atomically binds the job ID
   from the actor-generation request ID; Dify does not need a separate bind node.
4. Runner posts terminal results to `/v1/codex-completions`. The coordinator
   accepts a successful result only when the reported analyzed cursor equals the
   latest desired cursor, then invokes the configured short Dify result stage.
   A stale/failed/timed-out dirty result creates/reuses one continuation Runner
   job through the loopback Runner API instead of stranding the newer message.
5. The result stage calls `send_zendesk_native_message` with the same `run_token` and final
   snapshot. The relay authorizes at most one delivery attempt for that token.
6. For a terminal path that sends nothing, call `finish_zendesk_ticket_run`.

The lease defaults to 30 minutes, which is longer than one Dify Cloud workflow
run. If a worker crashes and stops renewing the lease, a later webhook can
claim a new generation after expiry.

Claim arguments:

```json
{
  "ticket_id": 2861,
  "event_id": "customer-message-123",
  "worker_id": "dify-workflow-run-id",
  "customer_last_message_id": "customer-message-123",
  "customer_message_count": 4,
  "customer_requested_human": false
}
```

Coordinated send arguments:

```json
{
  "ticket_id": 2861,
  "body": "Hello from remote MCP",
  "run_token": "token-returned-by-claim",
  "snapshot_customer_last_message_id": "customer-message-123",
  "snapshot_customer_message_count": 4,
  "handoff_type": "none",
  "freshness_check_url": "https://example.invalid/final-readiness",
  "freshness_check_api_key": "optional-bearer-key"
}
```

Legacy callers may omit `run_token`; this preserves compatibility for clients
that already implement their own durable public-delivery fence. Dify should
always use the coordinated form.

SSE discovery is available at:

```text
https://zendesk.smlershou.top/zendesk-reply-sse
```
