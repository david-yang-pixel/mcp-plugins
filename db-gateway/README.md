# db-gateway

从腾讯云当前 `db-gateway` 运行容器整理的独立 MCP 项目。端口：`18081`。全部业务代码位于本目录 `src/db_gateway/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --group dev
uv run --locked --group dev python -m pytest
cp .env.example .env
# 填写本地凭证后启动
uv run --locked db-gateway
```

使用 Python 3.12。需要 PostgreSQL 的 `psql` 和与环境匹配的 Teleport `tsh`；`tsh` 需在运行时安装或挂载。复制 `config.example.yaml` 为 `config.yaml` 后配置数据库代理。

## 查询工具 `db_gateway_query`

HTTP `POST /query` 与 MCP `tools/call`（`/mcp`，以及 `scripts/db_gateway_mcp.py` stdio 版本）使用同一套参数和返回结构。

### 参数

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `database` | 是 | `prod` / `billing` / `plugin` |
| `sql` | 是 | 单条只读查询，必须以 `SELECT` 或 `WITH` 开头，可带前置括号，例如 `(SELECT 1) UNION ALL (SELECT 2)`。末尾分号和注释会被去掉。 |
| `ticket_id` | 否* | Zendesk 工单号，写入审计日志。开启 `require_ticket_id` 后必填。 |
| `offset` | 否 | 跳过的行数，默认 `0`。翻页时传上一页的 `next_offset`。 |
| `limit` | 否 | 每页行数，默认且最大为 `postgres.max_rows`（默认 1000），超过时按上限返回并给出 warning。 |
| `format` | 否 | `csv`（默认）或 `json`。 |

分页不要在 SQL 里自己写 `LIMIT/OFFSET`，直接用 `offset`/`limit` 参数。只有原查询带顶层 `ORDER BY`（最好包含唯一键）时翻页结果才稳定；分页时如果原查询没有顶层 `ORDER BY`，返回里会带 warning。

### 返回字段

- `success`、`code`、`error`：失败时 `code` 为 `UNKNOWN_DATABASE`、`INVALID_ARGUMENT`、`TICKET_ID_REQUIRED`、`SQL_SAFETY_ERROR`、`TELEPORT_EXPIRED`、`QUERY_TIMEOUT` 或 `QUERY_ERROR`。
- `row_count`：本页真实数据行数，按 CSV 解析计数（多行字段算一行，没有 psql 的 `(N rows)` 页脚）。
- `total_rows`：结果总行数。无法得出时为 `null`，原因写在 `warnings` 里，例如计数超时。
- `has_more` / `truncated`：后面是否还有数据；`truncated` 是旧字段，值与 `has_more` 相同。
- `next_offset`：有下一页时为下一页的 `offset`，没有时为 `null`。`offset`、`limit` 返回实际生效的值。
- `columns`：列名数组。
- `format=csv`：`csv` 为表头加数据。psql CSV 中 **NULL 与空字符串都输出为空字段**，无法区分；需要区分时请用 `json`。
- `format=json`：`csv` 为空字符串，另返回 `rows`（**数组的数组**，与 `columns` 顺序一致，保留重名列）和 `column_types`（PostgreSQL 类型名，例如 `integer`、`timestamp with time zone`，由 psql `\gdesc` 获取，只解析不执行，额外一次往返）。值都是 PostgreSQL 文本表示的字符串（数字、`t`/`f`、JSON 文本等不做类型转换），SQL NULL 为 `null`，空字符串为 `""`。
- `warnings`：例如缺少 ticket_id、limit 被截断、分页缺少 ORDER BY、offset 超出总行数、total_rows 为 null 的原因。
- `elapsed_ms`：psql 进程耗时（旧字段）。`timing_ms` 为分阶段耗时，同时写入审计日志和结构化日志：
  - `queue_wait_ms`：等待本库并发槽位（`max_concurrent_queries`）。
  - `psql_spawn_ms`：启动 psql 子进程。
  - `psql_process_ms`：psql 进程从启动到退出的总耗时。
  - `connect_and_overhead_ms`：`psql_process_ms` 减去服务端执行时间的估算值，主要是 psql 启动、经 Teleport 本地代理建立连接（TLS/认证）和退出。
  - `describe_ms`：仅 json，获取列类型。
  - `query_ms`：建游标、跳过 offset、取本页（psql `\timing`）。
  - `count_ms`：计算 total_rows。
  - `parse_ms`：解析输出。
  - `total_ms`：网关内总耗时。
  - 发生 Teleport 重试时另有 `teleport_retry_ms` 和 `attempts`。

### 执行方式

每次查询启动一个 psql 进程，只建立一个连接，使用 `--csv -P footer=off`，NULL 用每次随机生成的标记区分。执行流程：

1. 在 `BEGIN TRANSACTION READ ONLY` 中设置 `statement_timeout = query_timeout_seconds`。
2. `DECLARE` 游标，内容为 `SELECT * FROM (<sql>) AS _db_gateway_q`；包装后的 SQL 会再次通过只读校验。
3. 依次执行 `MOVE offset`、`FETCH limit+1`；多取的一行只用来判断 `has_more`。
4. 在同一游标上执行 `MOVE ALL` 计算 `total_rows`，受 `count_timeout_seconds` 限制。这样不会再次执行原查询；超时只让 `total_rows` 为 `null`，本页数据照常返回。

用户 SQL 只出现在以 SQL 开头的 `-c` 参数中，psql 原样发给服务端，不解析其中的 `\` 元命令或 `:变量`。

### 连接方式 `postgres.driver`

- `psql`（默认）：每次查询启动一个 psql 进程并新建连接。经 `tsh proxy db --tunnel` 建连（TLS 到 Teleport、DB agent、Postgres 认证）约需 2 秒，`timing_ms.connect_and_overhead_ms` 主要就是这部分。
- `pool`：进程内用 psycopg 连接池为每个库（prod、billing、plugin）保持连接，平时一次查询只需自身的往返。执行方式与 psql 路径一致，每页固定两个往返：第一个是 `BEGIN READ ONLY; SET LOCAL statement_timeout; DECLARE; [MOVE offset;] FETCH`；第二个是 `[SET LOCAL statement_timeout; MOVE ALL;] SELECT pg_advisory_unlock_all(); ROLLBACK`。每个批次是一条简单查询消息，结束后一律回滚。值按 PostgreSQL 文本输出返回，csv/json 结果与 psql 路径逐字一致。`column_types` 用 `format_type()` 解析并缓存。只有页面取满（还有下一页）时才执行计数，否则总行数直接可知。

pool 模式与 Teleport 续签的配合：
- 证书由 `tsh-auth-watchdog` 自动续签：`tsh logout --all` 后用密码和 TOTP 重新 `tsh login`。之后 `tsh-cert.path` 重启 `tsh-proxy-db*` 隧道，经隧道的已有连接会全部断开。连接池不接触证书，只连接本机隧道端口。
- 连接池监视 `<teleport.auth_home>/keys` 下所有文件的修改时间，有变化就关闭所有连接池，下一次查询直接新建连接（`pool_watch_teleport_keys`）。
- 遇到连接级错误（连接被关闭、隧道拒绝连接、`pool_acquire_timeout_seconds` 内拿不到连接）时，清空该库的连接池并返回 `TELEPORT_EXPIRED`。核心的重试循环会间隔 `pool_retry_delay_seconds`（默认 1.5 秒，覆盖隧道重启时间）用新连接重试 `teleport_retry_attempts` 次。
- 空闲超过 `pool_max_idle_seconds`（240）的多余连接会被回收，常驻 `pool_min_size`（1）条；连接最长存活 `pool_max_lifetime_seconds`（1800）。空闲超过 `pool_check_idle_seconds`（60）的连接在交给查询前先探测一次，失效就换新连接。
- 连接数上限沿用 `max_concurrent_queries`；单飞（singleflight）和每库并发限制不变。
- 会话隔离：每次查询都在事务内执行并强制回滚，事务内做的会话级 `SET` / `set_config(..., false)` / `SET ROLE` 会随回滚撤销，不会被下一条查询继承。会话级 advisory lock 回滚后仍会保留，而且只读校验看不到所有取锁方式（例如 Unicode 转义标识符、`query_to_xml('...')`），所以每次查询的最后一个往返都会执行 `pg_advisory_unlock_all()`。查询中途出错时，先补发 `ROLLBACK` 和解锁再归还连接；如果清理也失败，就直接关闭这条连接。校验器拒绝 `pg_advisory_*` 只是第一道防线。

`timing_ms` 在 pool 模式下的字段：`pool_acquire_ms`（取得连接，含新建连接耗时）、`pool_new_connections`（本次新建的连接数，0 表示复用）、`query_ms`（第一个往返）、`count_ms` 或 `finish_ms`（第二个往返，带计数时为前者）、`describe_ms`（列类型首次出现时多一次往返，之后走缓存）、`parse_ms`。

切换方法：把 `deploy/runtime.json` 中 `settings.postgres.driver` 改为 `pool` 并部署 db-gateway；回退时改回 `psql`。

### 只读校验

校验器用词法分析识别注释、字符串、引号标识符和 `$$` 美元引号，然后检查：

- 未闭合的引号或注释会被拒绝。
- 只能有一条语句，分号只能出现在末尾。
- 查询（去掉前置括号后）必须以 `SELECT` 或 `WITH` 开头。
- 拒绝调用 `pg_advisory_*` / `pg_try_advisory_*` 函数（包括写成 `"pg_advisory_lock"` 的引号形式）：会话级 advisory lock 在回滚后仍留在连接上，pool 模式下会影响后续复用该连接的查询。
- 关键字黑名单：`INSERT`、`UPDATE`、`DELETE`、`DROP`、`SET`、`SHOW`、`COPY` 等。字符串常量里出现这些词也会保守拦截，错误信息会说明命中位置。

被拒绝的 SQL 返回具体原因，例如 `仅允许以 SELECT 或 WITH 开头的只读查询（可带前置括号），检测到: EXPLAIN`，并在审计日志中记为 `REJECTED_SQL_SAFETY`。

### 相关配置（`deploy/runtime.json` → `settings`，即 `config.yaml`）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `postgres.max_rows` | 1000 | 单页上限。 |
| `postgres.query_timeout_seconds` | 30 | 主查询 `statement_timeout`。psql 进程总超时为该值加 `count_timeout_seconds`。 |
| `postgres.count_timeout_seconds` | 5 | total_rows 计数预算，设为 `0` 时不计数（仍会通过 `has_more` 判断是否有下一页）。 |
| `db_gateway.require_ticket_id` | false | 设为 true 时，缺少 ticket_id 直接拒绝（`TICKET_ID_REQUIRED`）。也可用环境变量 `DB_GATEWAY_REQUIRE_TICKET_ID=true`。 |
| `db_gateway.missing_ticket_alert_interval_seconds` | 0 | 大于 0 时，缺少 ticket_id 会通过现有飞书机器人提醒，两次提醒至少间隔该秒数，期间的次数合并到下一条；为 0 时不提醒。 |

未开启 `require_ticket_id` 时，缺少 ticket_id 的查询照常执行，但返回 warning，审计日志记录 `"ticket_id_missing": true`。审计日志每条都带 `ticket_id_missing`、`offset`、`limit`、`format` 字段，成功时另有 `total_rows`、`has_more`、`timing_ms`。stdio 客户端默认超时 `DB_GATEWAY_TIMEOUT=45` 秒。

所有工具都在 `tools/list` 中声明 `annotations.readOnlyHint: true`、`destructiveHint: false`。OpenConnector 等客户端据此判断读写，未声明时新工具会被当作 write 并要求确认。

## 构建

```bash
docker build -t db-gateway:local .
```

Dockerfile 使用本目录完整源码和容器实际依赖版本，不依赖服务器上的旧镜像标签。生产配置、API 密钥、浏览器资料和业务数据应单独挂载，不能放入构建上下文。

## 版本来源

- 原镜像：`db-gateway:latest`。
- 镜像 ID：`sha256:378972148a4081b135ca2fdf3870bd36dbc04940fc38a8c185048efd4465887e`。
- `baseline/runtime.json` 记录实际依赖与容器来源；`baseline/source-manifest.json` 记录原始文件哈希。
- `requirements.runtime.txt` 固定运行依赖；`pyproject.toml` 通过 constraints 保持同样版本。原锁文件和部署文本保存在 `baseline/`，不作为当前部署入口。
- 原功能说明见 [baseline/README.original.md](baseline/README.original.md)。
