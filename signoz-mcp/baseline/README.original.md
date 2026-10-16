# Signoz MCP

独立的只读 SigNoz 查询服务。它在保留 `sz` CLI 原始查询能力的同时，提供面向排障的结构化 HTTP/SSE MCP 工具，供 Codex 和 `zd-agent` 调用。

## 边界

- `zd-agent` 只持有 `SIGNOZ_MCP_URL` 和 `SIGNOZ_MCP_API_KEY`。
- `SIGNOZ_URL` / `SIGNOZ_API_KEY` 只放在本服务自己的 `.env` 或服务器环境里。
- MCP 保留原始工具 `sz_run(args=[...])` 作为复杂查询的兜底入口。
- 常用排障通过结构化工具完成，调用方不需要记忆 ClickHouse 表名、时间类型和字段路径。
- 不提供 Alert、Dashboard、View、通知渠道等创建/修改/删除工具。
- 服务端统一追加 `--json -q`，返回结构化 JSON，失败不抛给调用方。

## 只读工具

| 工具 | 用途 |
| --- | --- |
| `signoz_list_services` | 发现活跃服务和 Kubernetes deployment |
| `signoz_get_field_keys` | 发现 logs/traces/metrics 可用字段 |
| `signoz_get_field_values` | 查看指定字段的高频值 |
| `signoz_search_logs` | 按环境、deployment、租户、级别、trace 或正文查日志 |
| `signoz_aggregate_logs` | 对日志计数、分组或按时间聚合 |
| `signoz_search_traces` | 按服务、operation、租户、错误和耗时查 span |
| `signoz_aggregate_traces` | 计算 Trace 数量、错误和 P50/P95/P99 延迟 |
| `signoz_get_trace_details` | 按 trace ID 还原完整 span 链路 |
| `signoz_list_metrics` | 发现可用指标及其元数据 |
| `signoz_query_metrics` | 按环境/deployment/Pod 查询和聚合指标 |
| `sz_run` | 运行无法由结构化工具表达的只读 `sz` 查询；拒绝多语句和写 SQL |

## 本地安装

```bash
cd /Users/yangyonggang/appdata/zd-mcp/signoz-mcp
uv sync --extra dev
cp .env.example .env
```

设置 `.env`：

```env
SIGNOZ_MCP_API_KEY=replace-with-a-long-random-token
SIGNOZ_MCP_HOST=127.0.0.1
SIGNOZ_MCP_PORT=8098
SIGNOZ_MCP_SZ_PATH=/Users/yangyonggang/appdata/zd-mcp/signoz-mcp/scripts/sz
SIGNOZ_MCP_TIMEOUT_SECONDS=35
SIGNOZ_MCP_MAX_CONCURRENT_RUNS=4
SIGNOZ_MCP_CACHE_TTL_SECONDS=300
SIGNOZ_MCP_CACHE_MAX_ENTRIES=128
SIGNOZ_MCP_CACHEABLE_COMMANDS=services,schema
SIGNOZ_MCP_SINGLEFLIGHT_ENABLED=true
SIGNOZ_URL=https://signoz.example.com
SIGNOZ_API_KEY=replace-with-signoz-api-key
```

`services` / `schema` 默认缓存 300 秒，完全相同的并发 `sz_run` 会合并为一次实际 `sz` 调用；其他命令只做并发合并和限流，不做持久缓存。

启动：

```bash
uv run signoz-mcp
```

## Agent 接入

`zd-agent` 的 `.env` 只需要：

```env
SIGNOZ_MCP_URL=https://<agent-public-domain>/signoz-mcp
SIGNOZ_MCP_API_KEY=<same-as-signoz-mcp>
```

## MCP 调用

```bash
curl -X POST http://127.0.0.1:8098/mcp \
  -H "Authorization: Bearer $SIGNOZ_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

```bash
curl -X POST http://127.0.0.1:8098/mcp \
  -H "Authorization: Bearer $SIGNOZ_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"sz_run","arguments":{"args":["services","--env","production","--hours","1","--limit","10"]}}}'
```

结构化调用示例：

```bash
curl -X POST http://127.0.0.1:8098/mcp \
  -H "Authorization: Bearer $SIGNOZ_MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"signoz_aggregate_traces","arguments":{"aggregation":"p95","aggregateOn":"duration_nano","groupBy":"attribute.dify.tenant_id","timeRange":"1h"}}}'
```

SSE discovery:

```bash
curl -H "Authorization: Bearer $SIGNOZ_MCP_API_KEY" http://127.0.0.1:8098/sse
```

## 服务器安装

```bash
sudo mkdir -p /opt/signoz-mcp/code /etc/signoz-mcp
sudo rsync -a --delete ./ /opt/signoz-mcp/code/
cd /opt/signoz-mcp/code
python3 -m venv .venv
.venv/bin/pip install -e .
sudo cp .env.example /etc/signoz-mcp/env
sudo chmod 600 /etc/signoz-mcp/env
sudo cp deploy/systemd/signoz-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now signoz-mcp
```
