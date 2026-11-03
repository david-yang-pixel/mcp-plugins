# signoz-mcp

从腾讯云当前 `signoz-mcp` 运行容器整理的独立 MCP 项目。端口：`8098`。全部业务代码位于本目录 `src/signoz_mcp/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest
cp .env.example .env
# 填写本地凭证后启动
uv run --locked signoz-mcp
```

使用 Python 3.12。本目录包含 `scripts/sz`；本地运行时设置 `SIGNOZ_MCP_SZ_PATH=./scripts/sz`。

## 工具用法

### 时间窗口：`timeRange` 与 `start`/`end`

所有带时间窗口的结构化工具（`signoz_list_services`、`signoz_get_trace_details`、`signoz_list_metrics`、`signoz_get_field_keys`、`signoz_get_field_values`、`signoz_search_logs`、`signoz_aggregate_logs`、`signoz_search_traces`、`signoz_aggregate_traces`、`signoz_query_metrics`）都支持：

- `timeRange`：相对窗口，如 `30m`、`2h`、`1d`。不超过 7 天时仍以 `sz --hours` 执行，行为不变；超过 7 天时换算为截止当前时间的绝对窗口；超过窗口上限时截断到上限，并在返回中标记 `clamped: true`。
- `start` / `end`：ISO 8601 绝对时间，如 `2026-10-01`、`2026-10-01T08:00:00`、`2026-10-01T16:00:00+08:00`。**不带时区按 UTC 处理**。
  - `start` + `end`：使用绝对窗口，**优先于 `timeRange`**（同时传入时返回 `timeRangeIgnored: true`）。
  - 只传 `start`：窗口为 `start` 到当前时间。
  - 只传 `end`：窗口为 `end` 前推 `timeRange`（缺省按工具默认值）。
  - 要求 `start < end`，窗口长度不超过 `SIGNOZ_MCP_MAX_WINDOW_DAYS`（默认 31 天）；否则返回 `BAD_ARGUMENTS`。
- 结构化工具返回中都带 `window`，回显解析后的 UTC `start`/`end`、`hours` 和 `mode`（`relative` 时 start/end 为请求时刻的近似值）。

### 长窗口分段查询

`signoz_aggregate_logs`、`signoz_aggregate_traces`、`signoz_query_metrics`、`signoz_list_services`、`signoz_get_field_values` 在窗口超过 `SIGNOZ_MCP_CHUNK_THRESHOLD_HOURS`（默认 48 小时）时，按 `SIGNOZ_MCP_CHUNK_HOURS`（默认 24 小时，内部边界对齐 UTC 零点）拆成多个子窗口，以 `SIGNOZ_MCP_CHUNK_CONCURRENCY`（默认 2）有限并发执行后合并：

- 可精确合并：`count`、`sum`（求和）、`min`、`max`、`avg`（子窗口返回 sum/count 后再相除）、指标 `latest`（取时间最新的样本）。分组和 `time_series` 按分组键/时间桶合并，最后按原规则排序并应用 `limit`。子窗口边界使用左闭右开，避免重复计数。
- 每个子窗口使用 `SIGNOZ_MCP_CHUNK_ROW_LIMIT`（默认 10000）行上限；任一子窗口达到上限时返回 `chunking.approximate: true`。
- `p50`…`p99` 与 `count_distinct` 无法跨子窗口合并，仍按单次查询执行，并在返回的 `chunking.reason` 中说明。
- 整体受 `SIGNOZ_MCP_TOTAL_TIMEOUT_SECONDS`（默认 50 秒）限制；单个子窗口仍受 `SIGNOZ_MCP_TIMEOUT_SECONDS` 限制。超时（`SZ_TIMEOUT` / `SZ_TOTAL_TIMEOUT`）时返回结构化错误，包含：`partial.data`（已完成子窗口的合并结果）、`partial.completedWindows` / `remainingWindows`，以及 `suggestion`（建议的最大窗口 `maxHours` 与可直接使用的 `start`/`end` 拆分 `windows`）。单次查询超时同样返回 `window` 与 `suggestion`。
- 日志和链路查询默认附加 `ts_bucket_start` 范围条件（SigNoz logs v2 / traces v3 主键首列），减少扫描量；如目标 SigNoz 版本不支持，可设置 `SIGNOZ_MCP_TS_BUCKET_PRUNING=false` 关闭。

### 日志结果精简：`fields`、`compact`、`maxFieldChars`

`signoz_search_logs` 新增：

- `fields`：只返回指定字段（数组或逗号分隔），可选 `timestamp_utc`、`id`、`trace_id`、`span_id`、`severity_text`、`body`、`attributes_string`、`resources_string`、`attribute.<键>`、`resource.<键>`，最多 30 个。字段选择在 SQL 层完成。
- `compact`（默认 `true`）：
  - 删除 `attributes_string` / `resources_string` 中位于 `SIGNOZ_MCP_LOG_COMPACT_DENYLIST` 的低价值键（默认包括 `host.ip`、`host.mac`、`host.id`、`os.*`、`process.*`、`telemetry.sdk.*`、`k8s.pod.uid`、`container.id`、`log.file.path` 等）以及空值，删除的键名汇总在 `compaction.droppedKeys`；
  - 相同长文本（如 body 和 `exception.stacktrace` 中重复的 traceback）只保留较长的一份，其余替换为 `[duplicate of body]` 这类标记；
  - 超过 `maxFieldChars`（默认 `SIGNOZ_MCP_LOG_MAX_FIELD_CHARS`=4000，范围 200–100000）的字符串保留开头和结尾，中间以 `…[truncated N chars]…` 标注。
  - `compact: false` 返回原始行。

### `sz_run`

`sz_run` 的工具描述列出全部允许的命令和参数（由 `src/signoz_mcp/sz_commands.py` 生成，与 `sz_runner` 白名单同源，测试会对照 `scripts/sz` 的 argparse 定义防止漂移）。传 `["help"]`、`["--help"]` 或 `["<命令>", "--help"]` 返回 JSON 命令列表，不会执行 sz。`errors`、`traceback`、`query`、`services` 支持 `--start`/`--end` 绝对时间（`query` 的 `{start_ns}`/`{end_ns}` 占位符随之替换）；不传时 `--hours` 行为与之前完全一致。`sz_run` 的返回格式保持 `{"success": true, "data": [...]}` 不变（source-mcp 依赖该格式）。

### 新增配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `SIGNOZ_MCP_MAX_WINDOW_DAYS` | `31` | 绝对窗口和长相对窗口上限（同时传给 sz 作为 `SZ_MAX_WINDOW_DAYS`） |
| `SIGNOZ_MCP_CHUNK_THRESHOLD_HOURS` | `48` | 超过该窗口长度时对可合并聚合分段查询 |
| `SIGNOZ_MCP_CHUNK_HOURS` | `24` | 子窗口长度 |
| `SIGNOZ_MCP_CHUNK_CONCURRENCY` | `2` | 单次请求内子窗口并发数（同时受 `SIGNOZ_MCP_MAX_CONCURRENT_RUNS` 限制） |
| `SIGNOZ_MCP_CHUNK_ROW_LIMIT` | `10000` | 每个子窗口的行上限 |
| `SIGNOZ_MCP_TOTAL_TIMEOUT_SECONDS` | `50` | 分段查询整体超时。线上路由前的代理读超时可能是 60 秒，整体预算加上终止子进程的约 5 秒宽限必须低于该值，否则客户端会收到网关 504 而不是部分结果；线上单次查询超时 `SIGNOZ_MCP_TIMEOUT_SECONDS` 为 45 秒 |
| `SIGNOZ_MCP_TS_BUCKET_PRUNING` | `true` | 是否附加 `ts_bucket_start` 条件 |
| `SIGNOZ_MCP_LOG_COMPACT_DENYLIST` | 见 `settings.py` | compact 时删除的属性键，逗号分隔 |
| `SIGNOZ_MCP_LOG_MAX_FIELD_CHARS` | `4000` | compact 时单个字符串截断长度 |

所有工具都在 `tools/list` 中声明 `annotations.readOnlyHint: true`、`destructiveHint: false`。OpenConnector 等客户端据此判断读写，未声明时新工具会被当作 write 并要求确认。

## 构建

```bash
docker build -t signoz-mcp:local .
```

Dockerfile 使用本目录完整源码和容器实际依赖版本，不依赖服务器上的旧镜像标签。生产配置、API 密钥、浏览器资料和业务数据应单独挂载，不能放入构建上下文。

## 版本来源

- 原镜像：`signoz-mcp:latest`。
- 镜像 ID：`sha256:85de4f2b94af5defd28ea15363566b66fec3bf8cc550dd9c43388c642980f2a0`。
- `baseline/runtime.json` 记录实际依赖与容器来源；`baseline/source-manifest.json` 记录原始文件哈希。
- `requirements.runtime.txt` 固定运行依赖；`pyproject.toml` 通过 constraints 保持同样版本。原锁文件和部署文本保存在 `baseline/`，不作为当前部署入口。
- 原功能说明见 [baseline/README.original.md](baseline/README.original.md)。
