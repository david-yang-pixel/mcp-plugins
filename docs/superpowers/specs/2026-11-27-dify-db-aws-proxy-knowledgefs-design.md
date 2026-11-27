# dify-db：常驻 tsh 代理加速 Lambda 日志，并接入 KnowledgeFS 生产只读库 设计

日期：2026-11-27
范围：`db-gateway`（OpenConnector 连接 `dify-db`）
前置：`2026-11-27-dify-db-teleport-read-tools-design.md`（已上线，§9.3）

## 1. 目的

1. **提速**：`plugin_lambda_logs` 每取一页都新起一次 `tsh aws` 加 aws CLI。启动约 0.6 秒，每次还要重新和 Teleport 握手，单页要 1.4–1.9 秒。改成常驻 `tsh proxy aws` 本地代理，日志查询在进程内用 botocore 发请求（即上一轮讨论的方案 B）。
2. **新数据源**：Teleport 里有一个能连但没用上的库 `knowledge-fs-production-readonly`（KnowledgeFS 生产 Aurora 只读端点）。把它接成 `db_gateway_query`、`db_gateway_schema` 的第 4 个库 `knowledgefs`，用于排查知识库文档的上传、解析、检索、删除问题。

用户已决定：做方案 B；KnowledgeFS 一起做。

## 2. 实测（2026-11-27 22:40 KST，`.tsh` 临时副本加一次性容器，用生产镜像）

| 项目 | 结果 |
| --- | --- |
| `tsh apps login` 后执行 `tsh proxy aws --app aws-support-logs` | 0.3 秒就绪。stdout 打印 `export AWS_ACCESS_KEY_ID=…`、`export AWS_SECRET_ACCESS_KEY=…`、`export AWS_CA_BUNDLE="<TELEPORT_HOME>/keys/<proxy>/<user>-app/<cluster>/aws-support-logs-localca.pem"`、`export HTTPS_PROXY="http://127.0.0.1:<随机端口>"` |
| botocore 1.44.12 经该代理 | 建客户端 0.08 秒；单页第一次 1.23 秒，之后 0.74–0.77 秒（现状 1.4–1.9 秒） |
| 3 天窗口，`"Traceback"`，`limit=10000` | 18.9 秒，6 页（其中 1 页为空），1129 条；每页 1.1–5.0 秒，主要是 CloudWatch 服务端扫描（现状约 38 秒） |
| Logs Insights `StartQuery` | AccessDenied（方案 C 不可行）；`DescribeLogGroups` 也被拒 |
| `tsh proxy db --tunnel --db-user=pro_ops_teleport_read --db-name=knowledgefs knowledge-fs-production-readonly` | 不用先 `tsh db login` 即可使用。库：`rdsadmin`、`postgres`、`knowledgefs`；`knowledgefs.public` 有 112 张表，如 `document_revisions`、`document_compilation_attempts`、`source_workflow_runs`、`answer_traces`、`deletion_jobs`、`upload_sessions`、`parse_artifacts` |
| `saas-02`（Redshift Serverless） | Teleport 数据库代理 `sts:AssumeRole` 被拒，属于 Teleport 管理员侧问题，不在范围内 |
| 主机端口 56064 | 空闲（现有隧道占 56061–56063） |

## 3. 设计

### 3.1 常驻 tsh 子进程（新模块 `tsh_daemons.py`）

`TshDaemon` 管理一个长期运行的 `tsh` 子进程：

- **`await ensure(cfg, deadline) -> dict`**（每个 daemon 一把锁）：
  1. `shadow = await ensure_shadow_home(cfg)`，`sig = source_signature(shadow)`。
  2. 进程仍在运行，且启动时记录的签名等于 `sig`（期间没有续期）→ 直接返回上次解析的信息。
  3. 否则停掉旧进程，执行启动前钩子，然后启动 `tsh <args>`：
     - 环境同 `run_tsh`（影子 `TELEPORT_HOME`、删除代理变量、`TELEPORT_TOOLS_VERSION=off`）；
     - `start_new_session=True`，stdout 和 stderr 合并到一个管道。
  4. 逐行读输出，直到就绪规则满足；超时为 `min(剩余时间, 15 秒)`。
     - 失败（进程退出或超时）→ 杀掉进程组，抛 `TshDaemonError`，消息附输出末尾 600 字符。
     - 成功 → 记录签名和解析结果，后台任务继续读完剩余输出（防止管道堵塞），只保留末尾若干行用于报错。
- **`mark_stale()`**：调用方发现代理异常时调用，下次 `ensure` 强制重启。
- **`stop()`**：杀进程组并回收。模块级 `stop_all()` 在 aiohttp `on_cleanup` 调用。
- 只写影子目录，不写线上 `.tsh`（与上一轮相同）。

两个实例：

| daemon | 参数 | 启动前钩子 | 就绪规则 / 解析 |
| --- | --- | --- | --- |
| `aws_proxy` | `proxy aws --app <lambda_logs.app>` | `tsh_login(["apps","login","--aws-role",<aws_role>,<app>])`（每次启动都做，约 2.4 秒，只在首次和续期后发生） | 四个 `export` 行齐全；解析出 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`、`AWS_CA_BUNDLE`、`HTTPS_PROXY`（去引号） |
| `knowledgefs_tunnel` | `proxy db --tunnel --port <port_knowledgefs> --db-user <user> --db-name <db_knowledgefs> <teleport_db>` | 无 | 输出出现 `127.0.0.1:<port>` |

### 3.2 `plugin_lambda_logs` 改用 botocore

- 新增 `aws_logs.get_logs_client(cfg, deadline)`：
  - 先 `aws_proxy.ensure`，再按解析结果缓存一个 botocore `logs` 客户端，代理重启后重建；
  - 客户端参数：`region`、凭据、`verify=AWS_CA_BUNDLE`、`proxies={"https": HTTPS_PROXY}`、`connect_timeout=10`、`read_timeout=30`、标准重试 `max_attempts=3`。
- 每页：`await asyncio.wait_for(asyncio.to_thread(client.filter_log_events, **params), 剩余时间)`。
  - 参数为 `logGroupName`、`startTime`、`endTime=end_ms-1`、`filterPattern`、`logStreamNames`、`limit=min(剩余条数, 10000)`、`nextToken`。
  - 不再拼命令行，所以过滤式以 `-` 开头的问题、aws CLI 打印前缀文字的问题都不存在了。
- 错误映射：
  - `ClientError` 且错误码为 `403`（本地代理拒绝，如签名或证书失效），或者连接、代理、SSL 类错误 → `aws_proxy.mark_stale()`，本页重试一次。
  - 其他 `ClientError` → `AWS_ERROR`，`error` 为 `Code: Message`。
  - 读超时或 `asyncio.TimeoutError` → 按 `timed_out` 处理，规则同前（第一页 `LOGS_TIMEOUT`，之后返回部分结果）。
  - `TshDaemonError` → 用宿主机 tsh 对源目录跑 `tsh status`：未登录 → `TELEPORT_EXPIRED`，已登录 → `NOT_AUTHORIZED`。
  - 登录钩子抛出的 `TeleportAuthExpired`、`TeleportNotAuthorized`、`TshTimeout` 照旧映射。
- 翻页规则、返回字段、警告、错误码保持不变。镜像去掉 aws CLI，改为固定版本的 botocore。

### 3.3 `knowledgefs` 数据库

- 配置：
  - `postgres.db_knowledgefs="knowledgefs"`、`postgres.port_knowledgefs=56064`；
  - `teleport_tools.knowledgefs`：`enabled=true`、`teleport_db="knowledge-fs-production-readonly"`、`db_user=""`（空时用 `postgres.user`，即 `pro_ops_teleport_read`）。
- `core`：
  - `_DB_KEY_TO_ATTR`、`_DB_KEY_TO_PORT_ATTR` 加 `knowledgefs`。
  - 在 `execute_gateway_query` 和 `execute_schema_lookup` 真正连库之前，对 `knowledgefs` 调 `ensure_knowledgefs_tunnel(cfg)`（截止时间 15 秒）。
  - 未启用 → `UNKNOWN_DATABASE`（说明未启用）。
  - 隧道失败：`TeleportAuthExpired` → `TELEPORT_EXPIRED`；其他 → `QUERY_ERROR`「KnowledgeFS 隧道启动失败：…」。
  - 判断启用用 `is True`，现有用 `MagicMock` 的测试不受影响。
- 两个数据库工具的 schema 枚举加 `knowledgefs`，描述写清来源。HTTP `/query`、`/schema` 和 stdio 自动支持（stdio 转发到 HTTP）。
- 续期后隧道重启，连接池里的旧连接会断，靠现有的连接失败重试（`TeleportAuthExpired` 加 `pool_retry_delay_seconds`）重连。

### 3.4 不做

- `kube_read` 不变（每次约 1 秒，大部分是网络）。
- 不做续期后预热（之前的方案 A），以后需要再加。
- `saas-02`、staging 库。

## 4. 测试

- `tsh_daemons`（用假 tsh 脚本）：
  - 解析就绪输出；
  - 同一签名复用进程，签名变化后重启；
  - 进程退出后重启；`mark_stale` 后重启；
  - 就绪前退出或超时 → `TshDaemonError`，附输出末尾，且杀掉进程组；
  - 启动前钩子的调用顺序；
  - 并发 `ensure` 只启动一次；
  - `stop_all` 杀掉进程。
- `plugin_lambda_logs`（用假 botocore 客户端）：
  - 保留原有行为用例：翻页、limit、预算、空页、错误、定位、审计、参数校验；
  - 代理 403 或连接错误 → 重启并重试一次；
  - `ClientError` → `AWS_ERROR`；
  - 每页超时 → `LOGS_TIMEOUT` 或部分结果；
  - `TshDaemonError` 的分类。
- `knowledgefs`：
  - `execute_gateway_query`、`execute_schema_lookup` 对 `knowledgefs` 先建隧道再查询；其他库不建隧道；
  - 未启用 → `UNKNOWN_DATABASE`；
  - 隧道失败的错误码；
  - schema 枚举。
- 镜像：构建后 `python -c "import botocore"` 成功；冒烟测试 `tools/list` 仍为 4 个工具。

## 5. 上线校验

1. `plugin_lambda_logs` 单页耗时（预期约 1 秒内，首次含登录除外），3 天 Traceback 翻完的总耗时（预期约 20 秒）。
2. `db_gateway_schema(knowledgefs, ["document_revisions"])` 和 `db_gateway_query(knowledgefs, "SELECT count(*) FROM public.logical_documents")` 成功。
3. 线上 `.tsh` 不出现 `-app`、`-kube`、knowledgefs 相关文件；主机 `127.0.0.1:56064` 由 db-gateway 容器内的 tsh 监听。
4. 续期后两个常驻进程自动重启，工具恢复，无需人工。
5. 其他容器不变。
