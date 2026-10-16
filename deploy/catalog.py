"""Service names and non-secret deployment metadata."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
SERVICES = (
    "db-gateway", "signoz-mcp", "source-mcp", "github-mcp",
    "docs-tool-service", "notification-service", "zendesk-mcp",
    "zendesk-browser-relay",
)
PORTS = dict(zip(SERVICES, (18081, 8098, 8099, 8111, 8112, 8113, 8100, 8097)))
AUTH_KEYS = dict(zip(SERVICES, (
    "DB_GATEWAY_API_KEY", "SIGNOZ_MCP_API_KEY", "SOURCE_MCP_API_KEY",
    "GITHUB_MCP_API_KEY", "DOCS_TOOL_SERVICE_API_KEY", "NOTIFICATION_SERVICE_API_KEY",
    "ZENDESK_MCP_API_KEY", "RELAY_API_KEY",
)))


def spec(service):
    if service not in SERVICES:
        raise ValueError("Unknown service")
    return json.loads((ROOT / service / "deploy" / "runtime.json").read_text())


def secret_name(service, key):
    return "MCP_" + service.upper().replace("-", "_") + "__" + key
