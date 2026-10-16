# sz — SigNoz CLI

零依赖 Python CLI，直接调 SigNoz v4 query_range API + ClickHouse SQL，绕过 `signoz-mcp-server` 与 v0.121 EE 后端的兼容问题。

## 使用

`sz` 已软链到 `~/bin/sz`，全局可用。源码：`signoz-cli/sz`。

### 子命令

| 命令 | 用途 | 示例 |
|---|---|---|
| `sz errors` | 搜 ERROR/FATAL 日志 | `sz errors --tenant <uuid> --hours 24 --limit 10` |
| `sz traceback` | 拉完整 Python stacktrace | `sz traceback --tenant <uuid> --hours 24 --limit 3` |
| `sz query "<SQL>"` | 任意 ClickHouse SQL | `sz query "SELECT count(*) FROM signoz_logs.distributed_logs_v2 WHERE timestamp >= {start_ns}" --hours 1` |
| `sz schema [table]` | 表结构（默认 `distributed_logs_v2`） | `sz schema` |
| `sz services` | 列活跃 service / deployment | `sz services --env production --hours 1` |

### 通用参数

- `--hours N` — lookback 窗口（默认 1，最大 168）
- `--limit N` — row 上限（默认 30，最大 1000）
- `--env E` — `deployment.environment` 过滤（`production` / `staging`）
- `--json` — 输出原始 JSON（适合 agent 管道处理）
- `-q` / `--quiet` — 屏蔽 stderr meta 行

### 关键设计点

- **`--tenant <uuid>`** 走 `attributes_string['tenant_id']` 精确匹配（dify 后端把 tenant_id 作为结构化 log attribute，不是 body 子串）
- **`--grep <substring>`** 走 `body LIKE '%substring%'`，适合按 dataset_id / 路径子串过滤
- **traceback 数据**在 `attributes_string['exception.stacktrace']` 字段，不在 body
- 时间字段是 UInt64 **纳秒**，SQL 用 `{start_ns}` / `{end_ns}` 占位符可自动替换
- 表名前缀 `signoz_logs.` 缺省自动补

## 配置

环境变量：

```bash
export SIGNOZ_URL="https://signoz.example.com"
export SIGNOZ_API_KEY="<your-key>"               # 必填，不要写入 git
```

## Agent 调用提示

给 agent 的 prompt 片段：

```
处理工单需要后端日志时，用 sz CLI（不是旧的 signoz-mcp-server，它对 EE 后端不兼容）：

  sz errors --tenant <tenant_id> --hours 24
  sz traceback --tenant <tenant_id> --hours 24 --limit 3
  sz errors --grep <dataset_id_or_path> --hours 6

不确定具体 tenant_id 时用 --grep 走 body 子串。
要看完整 Python stacktrace 用 traceback 子命令，不要用 errors（body 字段只有一行）。
要任意 SQL 用 sz query "<SQL>" --hours N，SQL 里用 {start_ns}/{end_ns} 占位符。
```

## 例：处理工单 #2643 的完整路径

```bash
# 1. 搜该 tenant 24h 内所有 ERROR
sz errors --tenant d9008497-b4f0-4993-9a65-61a87e084566 --hours 24

# 2. 看完整 stacktrace（这里 body 字段太短，必须用 traceback）
sz traceback --grep "datasets/33933658" --hours 24 --limit 1

# 3. 任意聚合统计
sz query "SELECT count(*) AS cnt, substring(body,1,80) AS body_head FROM signoz_logs.distributed_logs_v2 WHERE timestamp >= {start_ns} AND severity_text = 'ERROR' AND attributes_string['tenant_id'] = 'd9008497-b4f0-4993-9a65-61a87e084566' GROUP BY body_head ORDER BY cnt DESC" --hours 24
```

## 已知坑

- **Cloudflare CF code 1010**：Python urllib 默认 UA 被 CF ban，脚本已设置 `User-Agent: sz-cli/1.0` 绕过
- **`--env production` 不是好默认值**：很多 ERROR 日志没填 `deployment.environment` 属性，加默认 env 过滤会丢数据；`--env` 默认 = 不过滤
- **`tenant_id` 在 body 子串里不出现**：必须走 `attributes_string['tenant_id']` 精确匹配（`sz errors --tenant` 已经这样实现）
