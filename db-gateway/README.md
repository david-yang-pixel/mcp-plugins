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
| `database` | 是 | `prod` / `billing` / `plugin` / `knowledgefs`（见下文「KnowledgeFS 库」） |
| `sql` | 是 | 单条只读查询，必须以 `SELECT` 或 `WITH` 开头，可带前置括号，例如 `(SELECT 1) UNION ALL (SELECT 2)`。末尾分号和注释会被去掉。 |
| `offset` | 否 | 跳过的行数，默认 `0`。翻页时传上一页的 `next_offset`。 |
| `limit` | 否 | 每页行数，默认且最大为 `postgres.max_rows`（默认 1000），超过时按上限返回并给出 warning。 |
| `format` | 否 | `csv`（默认）或 `json`。 |
| `explain` | 否 | 布尔值，默认 `false`。为 `true` 时返回执行计划而不是数据，见下文。 |
| `count_total` | 否 | 布尔值，默认 `false`。为 `true` 且本页取满时，额外统计总行数填入 `total_rows`（多一个往返，受 `count_timeout_seconds` 限制）。翻页只需 `has_more` / `next_offset`，不需要总数时不要开。 |

`ticket_id` 参数已移除。旧调用方继续传入时会被静默忽略，不报错，也不再产生 warning；其他未知参数同样忽略。

分页不要在 SQL 里自己写 `LIMIT/OFFSET`，直接用 `offset`/`limit` 参数。只有原查询带顶层 `ORDER BY`（最好包含唯一键）时翻页结果才稳定；分页时如果原查询没有顶层 `ORDER BY`，返回里会带 warning。

### 返回字段

- `success`、`code`、`error`：失败时 `code` 为 `UNKNOWN_DATABASE`、`INVALID_ARGUMENT`、`SQL_SAFETY_ERROR`、`TELEPORT_EXPIRED`、`QUERY_TIMEOUT` 或 `QUERY_ERROR`。
  - `QUERY_ERROR`（pool 模式）的 `error` 与 psql 输出一致，包含 PostgreSQL 的 `ERROR`、`DETAIL`、`HINT`，以及出错位置：`POSITION`（相对调用方提交的 SQL，换算掉网关包装的前缀）、`LINE n:` 原文和 `^` 标记。例如列名写错时会带 `HINT:  Perhaps you meant to reference the column "workflow_runs.status".`，`uuid = text` 时会提示显式类型转换。psql 模式直接返回 psql 的错误输出（同样含 `HINT`，但 `LINE` 行号按包装后的语句计算，比原 SQL 多 1）。
  - `QUERY_TIMEOUT` 的 `error` 末尾附上查询涉及表（从 SQL 的 `FROM`/`JOIN` 中提取，排除 CTE 名）在 `pg_indexes` 中的现有索引定义，方便判断条件是否命中索引。索引列表在进程内按库缓存，最多每小时刷新一次；获取索引用单独的短超时（语句 2 秒，总预算 5 秒），失败时只记日志、60 秒内不重试，原超时错误保持不变。
- `row_count`：本页真实数据行数，按 CSV 解析计数（多行字段算一行，没有 psql 的 `(N rows)` 页脚）。
- `total_rows`：结果总行数。结果在本页内取完时为精确值；本页取满（`has_more=true`）时为 `null`，除非传了 `count_total=true` 且计数在预算内完成。为 `null` 时原因写在 `warnings` 里（未请求计数、计数超时等）。
- `has_more` / `truncated`：后面是否还有数据；`truncated` 是旧字段，值与 `has_more` 相同。
- `next_offset`：有下一页时为下一页的 `offset`，没有时为 `null`。`offset`、`limit` 返回实际生效的值。
- `columns`：列名数组。
- `format=csv`：`csv` 为表头加数据。psql CSV 中 **NULL 与空字符串都输出为空字段**，无法区分；需要区分时请用 `json`。
- `format=json`：`csv` 为空字符串，另返回 `rows`（**数组的数组**，与 `columns` 顺序一致，保留重名列）和 `column_types`（PostgreSQL 类型名，例如 `integer`、`timestamp with time zone`，由 psql `\gdesc` 获取，只解析不执行，额外一次往返）。值都是 PostgreSQL 文本表示的字符串（数字、`t`/`f`、JSON 文本等不做类型转换），SQL NULL 为 `null`，空字符串为 `""`。
- `warnings`：例如 limit 被截断、分页缺少 ORDER BY、offset 超出总行数、total_rows 为 null 的原因。
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
4. 仅当 `count_total=true` 时，在同一游标上执行 `MOVE ALL` 计算 `total_rows`，受 `count_timeout_seconds` 限制。这样不会再次执行原查询；超时只让 `total_rows` 为 `null`，本页数据照常返回。

用户 SQL 只出现在以 SQL 开头的 `-c` 参数中，psql 原样发给服务端，不解析其中的 `\` 元命令或 `:变量`。

### 执行计划 `explain=true`

SQL 先按原规则通过只读校验（调用方自己写 `EXPLAIN ...` 仍会被拒绝），然后由网关在校验后的语句前加上 `EXPLAIN `，在 `BEGIN TRANSACTION READ ONLY` + `statement_timeout` 中执行。不使用 `ANALYZE`，语句只做规划、不会真正执行。返回结构与普通查询相同：`columns` 为 `["QUERY PLAN"]`，每行计划是一行数据（`json` 时 `column_types` 为 `["text"]`）；`offset`/`limit` 在完整计划上分页，`total_rows` 为计划总行数。审计日志记 `"explain": true`。

### 连接方式 `postgres.driver`

- `psql`（默认）：每次查询启动一个 psql 进程并新建连接。经 `tsh proxy db --tunnel` 建连（TLS 到 Teleport、DB agent、Postgres 认证）约需 2 秒，`timing_ms.connect_and_overhead_ms` 主要就是这部分。
- `pool`：进程内用 psycopg 连接池为每个库（prod、billing、plugin）保持连接，平时一次查询只需自身的往返。执行方式与 psql 路径一致。默认（不计数）每页只有一个往返：`BEGIN READ ONLY; SET LOCAL statement_timeout; DECLARE; [MOVE offset;] FETCH; SELECT pg_advisory_unlock_all(); ROLLBACK` 作为一个批次发出，任一语句出错时后面的语句不会执行，由网关补发回滚和解锁。传 `count_total=true` 时为两个往返：第一个到 `FETCH` 为止；第二个是 `[SET LOCAL statement_timeout; MOVE ALL;] SELECT pg_advisory_unlock_all(); ROLLBACK`，只有页面取满时才执行计数。每个批次是一条简单查询消息，结束后一律回滚。值按 PostgreSQL 文本输出返回，csv/json 结果与 psql 路径逐字一致。`column_types` 用 `format_type()` 解析并缓存；不计数时这条网关内置的类型查询在回滚之后执行（只在类型首次出现时多一次往返）。连接池的连接不使用服务端预备语句（`prepare_threshold=None`），出错后不会残留失效的预备语句缓存。

pool 模式与 Teleport 续期的配合：
- 连接池不接触证书，只连接本机隧道端口。已建立的连接不受证书续期或过期影响（实测空闲 75 分钟、跨多次续期仍可用）；隧道本身断开时由下面的重试规则处理。
- db-gateway 自己的会话续期（见“Teleport 会话”）不需要连接池做任何事：自管隧道跟随 `current` 软链使用新证书，不重启；宿主机 `tsh-proxy-*` 隧道由宿主机 watchdog 续期，与网关无关。
- 后台保活：每 `pool_keepalive_seconds`（30）秒逐条探测一段时间没用过的空闲连接（空查询，超时 `pool_probe_timeout_seconds` 即 5 秒，超时直接关闭连接而不是等服务端取消）。探测成功即记为已验证，查询取连接时不再探测。保活每次只取一条、只在有空闲连接时取，取连接时不触发取连接探测，一般不会让连接池变大（查询恰好在探测期间到来时，连接池可能多建一条，空闲 240 秒后回收）。`pool_min_size=0` 的连接池不做保活，以免妨碍它缩到 0。要让“保活正常运行时取连接不探测”成立，`pool_check_idle_seconds` 需大于 2 × `pool_keepalive_seconds` − `pool_maintenance_interval_seconds`，并留些余量（默认 60 > 2 × 30 − 10 = 50）：保活只探测超过 keepalive − interval 秒没用过的连接，一条连接最长约 2 × keepalive − interval 秒不被验证。
- 从池里复用的连接在取出后 2 秒内出现连接级错误（连接在空闲时已被断开）：立即换新连接重试一次，不清空连接池；`timing_ms.pool_reconnects` 为 1。新建的连接出错不做立即重试（多半是语句或服务端把连接弄断了）。立即重试取连接时一律先探测（池里其他同时断开的连接会被丢弃，必要时新建），最多等 5 秒；在这条已验证或新建的连接上仍因连接断开失败时，核心的重试循环不再执行第三次，一条会弄断自身连接的语句每个请求最多执行两次。
- 其他连接级错误（隧道拒绝连接、`pool_acquire_timeout_seconds` 内拿不到连接、重试仍失败等）：清空该库的连接池并返回 `TELEPORT_EXPIRED`。核心的重试循环会间隔 `pool_retry_delay_seconds`（默认 1.5 秒，覆盖隧道重启时间）用新连接重试 `teleport_retry_attempts` 次。
- 空闲超过 `pool_max_idle_seconds`（240）的多余连接会被回收，常驻 `pool_min_size`（1）条；连接最长存活 `pool_max_lifetime_seconds`（1800）。超过 `pool_check_idle_seconds`（60）秒既没用过也没验证过的连接，在交给查询前先探测一次（保活正常运行时不会触发）。
- 连接数上限沿用 `max_concurrent_queries`；单飞（singleflight）和每库并发限制不变。
- 会话隔离：每次查询都在事务内执行并强制回滚，事务内做的会话级 `SET` / `set_config(..., false)` / `SET ROLE` 会随回滚撤销，不会被下一条查询继承。会话级 advisory lock 回滚后仍会保留，而且只读校验看不到所有取锁方式（例如 Unicode 转义标识符、`query_to_xml('...')`），所以每次查询的最后一个批次都会执行 `pg_advisory_unlock_all()`。查询中途出错时，先补发 `ROLLBACK` 和解锁再归还连接；如果清理也失败，就直接关闭这条连接。校验器拒绝 `pg_advisory_*` 只是第一道防线。

`timing_ms` 在 pool 模式下的字段：`pool_acquire_ms`（取得连接，含新建连接耗时）、`pool_new_connections`（本次新建的连接数，0 表示复用）、`query_ms`（第一个往返；不计数时就是整页唯一的往返）、`count_ms` 或 `finish_ms`（仅 `count_total=true` 时的第二个往返，执行了计数时为前者）、`describe_ms`（列类型首次出现时多一次往返，之后走缓存）、`parse_ms`、`pool_reconnects`（取到的连接已失效、换新连接重试的次数，0 或 1）。`pool_new_connections` 统计自该库上一次查询以来连接池新建的连接数，包括后台保活补建的连接。

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
| `postgres.query_timeout_seconds` | 30 | 主查询 `statement_timeout`。客户端总超时为该值，`count_total=true` 时再加 `count_timeout_seconds`（pool 模式另加 15 秒余量）。 |
| `postgres.pool_keepalive_seconds` | 30 | 后台保活间隔，`0` 关闭（关闭后取连接时按 `pool_check_idle_seconds` 探测）。 |
| `postgres.pool_probe_timeout_seconds` | 5 | 存活探测超时（最小 1 秒），超时的连接直接关闭。 |
| `postgres.pool_maintenance_interval_seconds` | 10 | 保活任务的检查间隔（最小 1 秒）。 |
| `postgres.count_timeout_seconds` | 5 | `count_total=true` 时的计数预算；设为 `0` 时关闭计数，`count_total` 不生效并给出 warning（仍会通过 `has_more` 判断是否有下一页）。 |
| `db_gateway.audit_log_dir` | 空 | 审计日志目录，文件名固定为 `db_gateway_audit.log`，目录不存在时自动创建；相对路径相对 `config.yaml` 所在目录。为空时使用 `<resources.base_dir>/<resources.state_dir>/logs`。生产配置为 `/var/lib/mcp/logs`（挂载 `@data/logs`，持久化）。 |

已移除的 `db_gateway.require_ticket_id`、`db_gateway.missing_ticket_alert_interval_seconds` 和环境变量 `DB_GATEWAY_REQUIRE_TICKET_ID` 不再生效；旧 `config.yaml` 中仍有这些键时照常加载并忽略。

审计日志每条都带 `offset`、`limit`、`format` 字段（`explain=true` 时另有 `explain`，`count_total=true` 时另有 `count_total`），成功时另有 `total_rows`、`has_more`、`timing_ms`；`db_gateway_schema` 的调用记为 `"tool": "db_gateway_schema"` 并带 `tables`。stdio 客户端默认超时 `DB_GATEWAY_TIMEOUT=45` 秒。

## 表结构工具 `db_gateway_schema`

MCP `tools/call`（`/mcp` 与 stdio 版本）和 HTTP `POST /schema` 使用同一套参数。

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `database` | 是 | `prod` / `billing` / `plugin` / `knowledgefs`（与 `db_gateway_query` 相同） |
| `tables` | 是 | 表名数组，最多 20 个，例如 `["workflow_runs"]`。可写 `schema.table`；未写 schema 时匹配所有非系统 schema（`public` 排在前面）；区分大小写的表名用双引号，例如 `"\"MyTable\""`。 |

返回 `tables`：每张表一项，含 `schema`、`table`、`columns`（`name`、`type`、`nullable`，有默认值时带 `default`）和 `indexes`（`name`、`definition`，即 `pg_indexes.indexdef`，主键索引名通常以 `_pkey` 结尾）。找不到的表放在 `not_found`，并在 `warnings` 中说明。失败时 `success=false`，`code` 为 `UNKNOWN_DATABASE`、`INVALID_ARGUMENT`、`TELEPORT_EXPIRED`、`QUERY_TIMEOUT` 或 `QUERY_ERROR`。

列来自 `information_schema.columns`，索引来自 `pg_indexes`。两条查询都是网关内置的只读 SQL，表名只作为绑定参数传入（`table_name = ANY(%s)`），在同一个 `READ ONLY` 事务中以 `statement_timeout`（`query_timeout_seconds` 与 15 秒取小）执行后回滚；`BEGIN`、两条查询和 `ROLLBACK` 用 psycopg pipeline 一次发出，只有一个往返（超时时附带索引的目录查询同样如此）。pool 模式复用连接池；psql 模式用 psycopg 临时建一条连接，用完关闭。类型名缩写（`varchar`、`timestamptz` 等）与 `scripts/dump_db_schema.py` 共用 `db_gateway.schema_info`。

所有工具都在 `tools/list` 中声明 `annotations.readOnlyHint: true`、`destructiveHint: false`。OpenConnector 等客户端据此判断读写，未声明时新工具会被当作 write 并要求确认。

## Teleport 会话（db-gateway 自管）

db-gateway 用镜像自带的 tsh 18.11.0（`teleport.tsh_path`，`/opt/teleport/bin/tsh`）自己登录 Teleport，不读写宿主机的 `.tsh`，也不再复制影子目录。容器里只有这一个 tsh 在用这个会话。

- **会话目录**：`teleport.sessions_dir`（`/tmp/db-gateway/tsh-sessions`）下每次续期新建 `gen-<N>/`，里面依次完成：
  1. `tsh login`（密码 + 由 `TELEPORT_TOTP_SECRET` 生成的 TOTP，pexpect 输入，输出里的密码和验证码会被打码）；
  2. `tsh kube login --all`，kubeconfig 写在 `gen-<N>/kubeconfig`；
  3. `tsh apps login --aws-role …`；
  4. `tsh status` 读取有效期和 Roles。

  全部完成后，用 `os.replace` 把相对软链 `current -> gen-<N>` 原子切换过去；旧目录保留 `session_retain_seconds`（90）秒后删除，让进行中的命令跑完。kube、apps 登录失败只记警告，不影响切换（用到时按需重登）。重启后第一次续期会清掉上次留下的 `gen-*`、`current` 和临时软链，目录里的其他文件不动。
- **谁用哪个路径**：
  - 常驻进程（隧道、AWS 代理）用 `TELEPORT_HOME=current/home`，切换后直接读到新证书，隧道不重启（Step 0 实测：证书过期后已运行的 `tsh proxy db --tunnel` 能用新证书建连）。AWS 代理的凭据在启动时取得，所以续期后会在后台换一个新代理进程；旧进程再保留 45 秒（长于一次工具调用的 40 秒预算、短于旧目录的 90 秒保留期）后停掉，在它上面开始的请求可以跑完。代理就绪时，把它写在 key ring 里的本地 CA（`<app>-localca.pem`）复制到 `/tmp/db-gateway/aws-proxy-ca/`，客户端改用这份副本。原因是其他 tsh 进程（每一代第一次 `tsh kubectl`、`tsh apps login`）会重写 key ring，与代理启动撞上时会把这个文件删掉；副本保留 10 分钟后清理。
  - `kube_read` 每次调用开始时取会话管理器当前的快照，锁定它的 `gen-<N>`（即当时 `current` 指向的目录），整次调用都用这一代的目录和 kubeconfig；中途续期不会打断它。
- **续期时机**（`maintenance.py`，后台任务）：启动后立即在后台首次登录，不阻塞启动、`/healthz`、`/mcp`；之后在到期前 `renew_before_seconds`（480）秒续期。Teleport 把证书有效期限制在 1 小时，所以大约每 50 分钟续期一次。请求到来时会话不可用，会按需登录一次，并发请求共用这次登录；按需登录连同 kube、apps 登录都只用请求剩余的时间，来不及的子登录跳过，由工具用到时再做。后台续期排队等锁时，如果会话已被按需或人工登录更新，就跳过这次续期。
- **失败处理**：
  - 网络或集群错误：从 `renew_retry_seconds`（30）秒开始指数退避，最长 5 分钟一次。连续失败 3 次（退避 30、60 秒后的第 3 次，约 2 分钟）且会话已不可用时发飞书告警（按 `notify_interval_seconds` 限频），部署重启时的短暂抖动不会告警。
  - 密码或验证码被拒：等下一个 30 秒 TOTP 窗口重试一次。同一个窗口的验证码只提交一次（包括人工登录用过的）：按需登录来不及等下一个窗口时直接失败，不再提交，也不计入被拒次数。连续被拒 `max_auth_failures`（2）次，或 Teleport 回复账号锁定时，停止自动登录，避免把账号锁住。之后每 `notify_interval_seconds`（3600）秒发一次飞书告警，附一次性认证链接（60 分钟有效）。在链接页面手动登录成功后，自动续期恢复。
  - 手动登录时输错验证码不计入上述次数，也不会停止自动登录。
- **没有凭据时**（`TELEPORT_PASSWORD` 或 `TELEPORT_TOTP_SECRET` 为空，或 `auto_renew: false`）：不自动登录。若配置了 `teleport.proxy` 和 `TELEPORT_USER`，会话缺失或离到期不足 `renew_before_seconds` 时，按 `notify_interval_seconds` 发附一次性链接的飞书告警，请人工登录；两者缺一（例如 CI 冒烟测试）则什么都不做。会话不可用时，用到 Teleport 的调用（包括走自管隧道的数据库查询）返回 `TELEPORT_EXPIRED`。
- **立即续期 `POST /teleport-session/renew`**（需 API 密钥；公网地址为 `/db-gateway/teleport-session/renew`）：用已配置的凭据马上续期一次，流程与定时续期完全相同（新建 `gen-<N>`、切换 `current`、AWS 代理换新），返回 `previous_generation`、`generation`、`valid_until`、`roles`、`elapsed_ms`。用于验证续期，或在更新密码 Secret、调整 Teleport 角色后立即生效。
  - 每次都是一次真实的 Teleport 登录：同一时间只处理一个请求，另一个请求返回 409；同一个 TOTP 窗口刚用过时，会先等下一个窗口（最多约 30 秒）。
  - 自动登录已停止时不会绕过，返回 409，需要先通过 `/teleport-auth` 人工登录。网络类失败返回 503。

  ```bash
  curl -s -X POST -H "X-API-Key: $DB_GATEWAY_API_KEY" http://127.0.0.1:18081/teleport-session/renew
  ```
- **`/status`**（需 API 密钥）：
  - `teleport_session`：代数、有效期、剩余秒数、Roles、是否停止自动登录及原因、连续被拒次数、最近一次错误；
  - `databases`：每个库走 db-gateway 自管隧道（`via: db-gateway`）还是宿主机隧道的固定端口（`via: static`）。

  返回内容不含任何凭据。

### 人工登录页 `/teleport-auth`

一次性 token 记录它续期的是哪个会话：

| 来源 | 续期对象 |
| --- | --- |
| db-gateway 停止自动登录时的飞书告警 | db-gateway 自管会话（`SESSION.renew`，60 分钟有效） |
| 宿主机 `tsh-auth-watchdog` 调用 `/internal/teleport-auth/issue`（`X-Internal-Secret`，常量时间比较） | 宿主机会话 `teleport.auth_home`，用宿主机挂载的 tsh 登录（5 分钟有效） |

宿主机会话这条路径在 Step 3 停用宿主机 watchdog 和隧道之前保留。登录页会显示续期对象。登录成功后 token 失效；密码或验证码错误时 token 保留，但每个链接最多失败 3 次（每次失败都计入 Teleport 自己的锁号计数）。同一时间只处理一次登录，重复提交会被拒绝。

### 数据库隧道

四个库都由 db-gateway 自管隧道连接（生产配置见 `deploy/runtime.json`）：

| 库 | Teleport 数据库 / 库名 | 自管隧道端口 | 回退用的宿主机隧道端口 |
| --- | --- | --- | --- |
| `knowledgefs` | `teleport_tools.knowledgefs.teleport_db`（`knowledge-fs-production-readonly`）/ `knowledgefs` | `port_knowledgefs`（56064） | 无 |
| `prod` | `postgres.teleport_db_prod`（`lg-pgsql-read-01`）/ `langgenius` | `tunnel_port_prod`（56071） | `port_prod`（56061） |
| `billing` | `postgres.teleport_db_billing`（`lg-billing-01`）/ `billing` | `tunnel_port_billing`（56072） | `port_billing`（56062） |
| `plugin` | `postgres.teleport_db_plugin`（`lg-pgsql-read-01`）/ `dify_plugin_daemon` | `tunnel_port_plugin`（56073） | `port_plugin`（56063） |

- 填写了 `postgres.teleport_db_<库>` 的库，使用 db-gateway 自管的 `tsh proxy db --tunnel --db-user <postgres.user> --db-name <库名> --port <tunnel_port_<库>>`；参数与原宿主机 `tsh-proxy-*` 容器相同。
- 回退：清空某个库的 `teleport_db_<库>` 并部署，这个库就回到宿主机隧道的 `port_<库>`。宿主机隧道在 Step 3 停用之前一直运行。
- 自动登录停止或会话不可用时，四个库都会返回 `TELEPORT_EXPIRED`，此时需要按“失败处理”恢复会话，或临时回退到宿主机隧道。
- `plugin_lambda_logs` 按工作空间查插件库时走同一套选择逻辑。
- 自管隧道在第一次查询时启动：进程就绪最多等 15 秒，连同可能的按需登录，准备阶段最多 40 秒。进程退出时由下一次查询重启，并清空该端口的连接池。
- 启动失败时：会话不可用返回 `TELEPORT_EXPIRED`，其他原因返回 `QUERY_ERROR`。

### 配置（`settings.teleport`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `proxy` | 空 | Teleport 代理地址。 |
| `tsh_path` | `/opt/teleport/bin/tsh` | 所有自管会话、隧道和工具使用的 tsh。 |
| `sessions_dir` | `/tmp/db-gateway/tsh-sessions` | `gen-<N>` 和 `current` 所在目录，在容器内，重启后重建。 |
| `auto_renew` | `true` | `false` 时不自动登录，到期前用飞书告警里的链接人工登录。 |
| `renew_before_seconds` | `480` | 到期前多少秒续期。 |
| `renew_retry_seconds` | `30` | 网络类失败的首次重试间隔（指数退避，最长 300 秒）。 |
| `session_retain_seconds` | `90` | 旧 `gen-<N>` 保留时间。 |
| `max_auth_failures` | `2` | 连续被拒多少次后停止自动登录。 |
| `login_timeout_seconds` | `60` | 单次 `tsh login` 超时。 |
| `notify_interval_seconds` | `3600` | 同类飞书告警的最短间隔。 |
| `auth_home` | 空 | 宿主机会话目录，只给宿主机 token 的人工登录和旧脚本用。 |

凭据来自环境变量 `TELEPORT_USER`、`TELEPORT_PASSWORD`、`TELEPORT_TOTP_SECRET`（GitHub Secrets），不出现在配置、日志或 `/status` 里。

## Teleport 只读工具 `plugin_lambda_logs`、`kube_read`

使用网关自管的 Teleport 会话（见上文），查 SaaS 插件 Lambda 的 CloudWatch 日志和只读查看 Kubernetes 集群。

两个工具只走 MCP `/mcp`：没有 HTTP 端点，stdio 版本（`scripts/db_gateway_mcp.py`）也不转发，仍只列出两个数据库工具。所有访问在 Teleport 和 CloudTrail 里记在网关的 Teleport 账号名下。

### 运行方式

- **tsh**：镜像自带的 `teleport.tsh_path`。宿主机挂载的 `/usr/local/bin/tsh` 是 18.0.0，它的 `tsh aws` 对 18.11 的 proxy 签名校验失败（403），只在宿主机会话的人工登录里使用。
- **常驻 tsh 子进程**（`tsh_daemons.py`）：
  - 日志工具用一个常驻的 `tsh proxy aws --app <app>` 本地代理，启动前先 `tsh apps login --aws-role <aws_role> <app>`；查询在进程内用 botocore 经这个代理发出。单页约 0.75 秒，以前每页起一次 `tsh aws` 加 aws CLI 要 1.4–1.9 秒。
  - 自管的数据库隧道（见“数据库隧道”）。
  - 都在首次使用时启动；进程退出或代理拒绝请求时，在下一次调用时重启；网关退出时整组杀掉。续期时隧道不重启，AWS 代理在后台换新。它们的输出由后台任务持续读取并保留末尾 50 行，启动失败时附在错误里。
- **子进程**：参数列表方式执行，不经 shell。tsh 运行在独立进程组里，到截止时间整组杀掉。环境里删除 `HTTP(S)_PROXY`、`ALL_PROXY`、`NO_PROXY`，设置 `TELEPORT_HOME`（常驻进程用 `current/home`，`kube_read` 用锁定的 `gen-<N>/home`）、`KUBECONFIG`（对应的 `kubeconfig`）、`TELEPORT_TOOLS_VERSION=off`（关闭 tsh 客户端自动更新）。
- **时间预算**：整次工具调用共用一个截止时间（`time_budget_seconds`，默认 40 秒），SQL 定位、登录和每次 tsh 调用都只用剩余时间，保证低于 Nginx 的 60 秒超时。
- **按需登录**：
  - 集群工具在 kube 证书丢失时由 `tsh kubectl` 自动重签；仍失败则执行 `tsh kube login --all` 后重试一次，并发调用共享这一次登录。
  - 日志工具的代理返回 403 或连接失败时，重启代理（含 `apps login`）并重试本页一次。
  - 登录或代理启动失败时，按当前会话判断：会话不可用返回 `TELEPORT_EXPIRED`（续期后重试），会话有效返回 `NOT_AUTHORIZED`，并附当前 Roles。
- 审计日志 `db_gateway_audit.log` 记录工具名、状态、耗时、函数名/窗口/过滤条件/事件数，或集群/完整参数/退出码/输出字节数；不记录日志和 kubectl 输出内容。

### `plugin_lambda_logs`

调用 CloudWatch Logs `FilterLogEvents`（日志组 `/aws/lambda/<函数名>`，区域 `<region>`），与手工命令 `tsh aws --app <app> logs filter-log-events …` 等价，但经常驻代理加 botocore 发出。镜像不再包含 aws CLI。

| 参数 | 说明 |
| --- | --- |
| `function_name` | 函数名（含 `--2` 等后缀），`^[A-Za-z0-9_-]{1,64}$`。与下面两项二选一。 |
| `tenant_id` + `plugin_id` | 工作空间 UUID 和 `publisher/name`。网关查 `plugin_installations` LEFT JOIN `serverless_runtimes` 得到函数名；安装表反映当前状态，故障后升级或重装过要按故障时的版本查。 |
| `start_time` + `end_time` | ISO 8601 或毫秒时间戳；不带时区按 `default_timezone`（北京时间）；`end_time` 不含端点；小于 1e11 的数字视为秒级时间戳并拒绝。与 `since_minutes` 二选一。 |
| `since_minutes` | 最近 N 分钟，无上限。 |
| `filter_pattern` | CloudWatch 过滤语法，原样透传，例如 `"ERROR"`、`"Traceback"`、`"<RequestId>"`。 |
| `log_stream_names` | 只查这些日志流。 |
| `limit` | 本次最多返回的事件数，默认 1000，最大 10000。 |
| `next_token` | 上次返回的翻页令牌。续页时用上次返回的 `window.start_ms`、`window.end_ms` 作为 `start_time`、`end_time`；不能与 `since_minutes` 同时使用（相对时间每次都会变，令牌会对上不同的窗口）。 |

返回 `function_name`、`log_group`、`region`、`window`（`start_ms`、`end_ms`、`start_utc`、`end_utc`、`start_local`、`end_local`、`timezone`）、`events`（`timestamp`、`log_stream`、`event_id`、`message`，内容不截断）、`event_count`、`next_token`、`complete`、`warnings`；按工作空间定位时另带 `plugin_unique_identifier`。

翻页规则：
- 取完返回 `complete=true`。
- 达到 `limit` 或用掉 60% 时间预算时返回已取到的事件和 `next_token`，用相同参数加 `next_token` 继续。
- CloudWatch 可能连续返回空页但带令牌，网关会继续翻。
- 后续页失败时返回已取到的部分并说明原因。

### `kube_read`

执行 `tsh kubectl <子命令> --context <集群 context> <其余参数>`。context 从本次锁定的 `gen-<N>/kubeconfig` 里按 `<proxy 主机>-<集群名>` 匹配，找不到时先 `tsh kube login --all` 生成。

| 参数 | 说明 |
| --- | --- |
| `cluster` | `kube.clusters` 里的别名，默认 `prod`（`lg-eks-01`）、`staging`（`dify-plugin-1`）。 |
| `args` | kubectl 参数数组（不含 `kubectl`），例如 `["get","deploy","-n","dify-plugin","-o","wide"]`。 |

拒绝规则（防误操作和换身份，不是安全边界；能读哪些资源由 Teleport 角色决定）：
- 第一个参数必须是 `get`、`describe`、`logs`、`top`、`explain`、`api-resources`、`api-versions`、`version` 或 `auth can-i`。
- 拒绝 `-f`/`--filename`、`-k`/`--kustomize`、`--kubeconfig`、`--context`、`--cluster`、`--user`、`--as*`、`--token`、`--server`/`-s`、证书和 TLS 参数、`--follow`、`-w`/`--watch`/`--watch-only`、`--cache-dir`、`--profile*`。
- 短参数连写也会检查（`-Aw` 被拒，`-nfoo`、`-owide` 放行）；第一个 `--` 之后不检查。

返回 `cluster`、`context`、`output`（stdout 原文）、`stderr`、`exit_code`、`truncated`、`timed_out`、`warnings`。到时间预算时：有部分输出就返回成功并标 `timed_out`；一点输出都没有则返回 `TSH_TIMEOUT`。

`tsh kubectl` 即使 kubectl 出错退出码也是 0，所以 stderr 里有以 `error:`、`Error from server`、`Error in configuration`、`Unable to connect to the server` 开头的行时，返回 `success=false`、`KUBECTL_ERROR`，`output` 照常返回；`No resources found …` 不算错误。输出超过 `kube.max_output_bytes` 时只保留开头并标 `truncated`。

### 错误码

| 错误码 | 含义 |
| --- | --- |
| `INVALID_ARGUMENT` | 参数不合法（不写审计） |
| `TELEPORT_EXPIRED` | Teleport 会话不可用（未登录或正在续期），稍后重试 |
| `NOT_AUTHORIZED` | 会话有效但角色不允许或看不到 app/集群，附当前 Roles |
| `UNKNOWN_FUNCTION` | 按工作空间定位时没有安装、没有运行映射或后端不是 Lambda |
| `QUERY_ERROR` | 查插件库失败或超时 |
| `AWS_ERROR` | 第一页 aws 报错（附 stderr） |
| `LOGS_TIMEOUT` | 第一页就超过时间预算，一条都没取到 |
| `KUBECTL_ERROR` | kubectl 报错（stderr 有错误行或退出码非 0） |
| `TSH_TIMEOUT` | 时间预算在 tsh 完成前用完：等待并发槽位、登录超时、常驻代理没有及时就绪、还没开始查询，或 kubectl 没有任何输出；稍后重试 |

### 配置（`settings.teleport_tools`）

| 键 | 默认 |
| --- | --- |
| `time_budget_seconds` | `40` |
| `max_concurrent` | `3`（每个工具各自的并发上限；按工作空间定位时的插件库查询另外还占 `plugin` 库的查询槽位） |
| `lambda_logs.enabled` | `true`；`false` 时工具不出现在 `tools/list` |
| `lambda_logs.app` / `aws_role` / `region` | `aws-support-logs` / `SupportLambdaLogsReadOnly` / `us-east-1` |
| `lambda_logs.default_timezone` | `Asia/Shanghai` |
| `lambda_logs.default_limit` / `max_limit` | `1000` / `10000` |
| `kube.enabled` | `true` |
| `kube.clusters` | `{"prod": "lg-eks-01", "staging": "dify-plugin-1"}` |
| `kube.max_output_bytes` | `1048576` |
| `knowledgefs.enabled` | `true`；`false` 时 `knowledgefs` 库返回 `UNKNOWN_DATABASE` |
| `knowledgefs.teleport_db` | `knowledge-fs-production-readonly` |

已移除的 `teleport_tools.tsh_path`、`teleport_tools.shadow_home`、`kube.kubeconfig_path`、`postgres.pool_watch_teleport_keys` 不再生效；旧 `config.yaml` 中仍有这些键时照常加载并忽略。

## KnowledgeFS 库 `knowledgefs`

`db_gateway_query` 和 `db_gateway_schema` 的第 4 个库，指向 Teleport 数据库 `knowledge-fs-production-readonly`（KnowledgeFS 生产 Aurora 只读端点；库 `knowledgefs`，账号 `postgres.user`），用于排查知识库文档的上传、解析和编译、来源工作流、检索回答链路和删除问题。常用表有：
- 文档：`logical_documents`、`document_revisions`、`document_compilation_attempts`、`parse_artifacts`、`upload_sessions`；
- 来源工作流：`source_workflow_runs`；
- 检索回答：`answer_traces`、`answer_trace_steps`；
- 删除：`deletion_jobs`、`deletion_job_items`。

它的隧道一直由 db-gateway 自管：`tsh proxy db --tunnel --port <postgres.port_knowledgefs> …`（默认 `127.0.0.1:56064`，host 网络），行为见“数据库隧道”。续期时隧道不重启，已有连接继续可用。

只读校验、分页、超时、审计和其他库一致。

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
