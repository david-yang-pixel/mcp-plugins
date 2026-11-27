# dify-db 常驻 tsh 代理（Lambda 日志提速）+ KnowledgeFS 库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) to implement this plan task-by-task.

**Goal:** 两件事：
- `plugin_lambda_logs` 改为常驻 `tsh proxy aws` 加进程内 botocore，单页约 0.75 秒，3 天稀疏查询约 20 秒；
- 新增数据库 `knowledgefs`（Teleport `knowledge-fs-production-readonly`），隧道由 db-gateway 在容器内常驻管理。

**Spec:** `docs/superpowers/specs/2026-11-27-dify-db-aws-proxy-knowledgefs-design.md`

## Global Constraints

- 沿用上一轮的全部约束（只改 `db-gateway/`；不改端口映射、网络、挂载、Secret；不写线上 `.tsh`；参数列表执行、不经 shell；统一截止时间；审计不写内容）。
- 常驻进程用 `teleport_cli._tsh_env(cfg)` 的环境、`start_new_session=True`，停止时杀进程组。
- 判断开关一律用 `is True`，让现有 `MagicMock` 配置的测试保持不变。
- 提交信息结尾附 Co-Authored-By 和 Claude-Session；推送到 `main` 需要用户授权。

## Review Focus

1. 续期（影子签名变化）后，两个常驻进程会重启；旧进程被杀掉，没有泄漏。
2. 代理返回 403 或连接失败时，只重启并重试一次，不会无限循环；重试也受截止时间约束。
3. 常驻进程的输出管道一直有后台任务在读，进程不会因为写满管道而卡住。
4. 只有 `knowledgefs` 会建隧道，其他库的查询路径完全不变。
5. 线上 `.tsh` 仍然不被写。

---

### Task 1: 配置

- `PostgresConfig` 加 `db_knowledgefs: str = "knowledgefs"`、`port_knowledgefs: int = 56064`。
- `TeleportToolsConfig` 加 `knowledgefs: KnowledgeFsConfig`，字段为 `enabled=True`、`teleport_db="knowledge-fs-production-readonly"`、`db_user=""`。
- `deploy/runtime.json`：`settings.postgres` 加 `db_knowledgefs`、`port_knowledgefs`；`settings.teleport_tools` 加 `knowledgefs.enabled=true`。
- `config.example.yaml` 同步。
- 测试（`test_config.py`）：默认值；runtime.json 能加载并启用 knowledgefs。

### Task 2: `tsh_daemons.py`

接口：

```python
class TshDaemonError(Exception): ...
class TshDaemon:
    def __init__(self, name: str, *, args: Callable[[cfg], list[str]],
                 parse: Callable[[list[str]], dict | None],
                 before_start: Callable[[cfg, float], Awaitable[None]] | None = None,
                 start_timeout: float = 15.0): ...
    async def ensure(self, cfg, deadline: float) -> dict
    def mark_stale(self) -> None
    async def stop(self) -> None
async def stop_all() -> None
def _reset_state() -> None        # 测试：停掉并清空全部实例的状态
```

- `parse(lines)` 收到目前为止的全部输出行，返回 dict 表示已就绪，返回 None 表示继续等。
- `ensure` 的流程按 spec §3.1。

测试（`test_tsh_daemons.py`，用 FakeTsh）：
- `test_ensure_parses_ready_output_and_reuses_process`
- `test_restart_after_shadow_signature_change`
- `test_restart_after_process_exit`
- `test_mark_stale_forces_restart`
- `test_exit_before_ready_raises_with_output_tail`
- `test_ready_timeout_kills_process_group`
- `test_before_start_runs_first`
- `test_concurrent_ensure_starts_once`
- `test_stop_all_kills_processes`

FakeTsh 需要支持「先输出就绪行再长时间 sleep」，已有 `push(stdout=…, sleep=…)` 可以满足。

### Task 3: AWS 代理和 botocore 客户端，`lambda_logs` 改造

- 新模块 `aws_logs.py`：
  - `AWS_PROXY = TshDaemon("aws_proxy", …)`；
  - `parse_proxy_env(lines)`；
  - `async def get_logs_client(cfg, deadline)`，内部缓存 session 和 client；
  - `is_proxy_failure(exc) -> bool`。
- `lambda_logs.py`：
  - 翻页改为 `await asyncio.wait_for(asyncio.to_thread(client.filter_log_events, **params), remaining)`；
  - 删除 argv 拼装、`_parse_aws_json`、`_needs_login`；
  - 错误映射按 spec §3.2。
- 测试：
  - `test_lambda_logs.py` 改为假客户端：monkeypatch `db_gateway.lambda_logs.get_logs_client`，返回带响应队列的 `FakeLogsClient`。原有行为用例全部保留并改写。
  - 新增用例：
    - `test_proxy_403_restarts_proxy_and_retries_once`
    - `test_connection_error_retries_once_then_aws_error`
    - `test_client_error_is_aws_error_with_code`
    - `test_page_timeout_first_page_is_logs_timeout`
    - `test_daemon_error_classified`
  - `test_aws_logs.py`：`parse_proxy_env`（含引号）；`get_logs_client` 在代理重启后重建客户端。

### Task 4: knowledgefs

- 新函数 `tsh_daemons.ensure_knowledgefs_tunnel(cfg, deadline)`，以及实例 `KNOWLEDGEFS_TUNNEL`。
- `core.py`：
  - 两个映射表加 `knowledgefs`；
  - 新增 `async def _prepare_database(cfg, key) -> tuple[code, error] | None`，在 `execute_gateway_query` 的 SQL 校验之后、`execute_schema_lookup` 查询之前调用；
  - 未启用 → `UNKNOWN_DATABASE`。
- `mcp_schema.py`：两个工具的枚举和描述加 `knowledgefs`。
- `app.py`：`/status` 的 ports 加 knowledgefs；`on_cleanup` 调用 `tsh_daemons.stop_all()`。
- 测试：
  - `test_knowledgefs_query_starts_tunnel_first`
  - `test_other_databases_do_not_start_tunnel`
  - `test_knowledgefs_disabled_is_unknown_database`
  - `test_knowledgefs_tunnel_failure_codes`
  - `test_schema_lookup_knowledgefs`
  - `test_tools_list_database_enum_has_knowledgefs`
  - stdio 测试不受影响。

### Task 5: 依赖、README、镜像

- 依赖：去掉 `awscli`，加 `botocore`（固定版本）。同步 `pyproject` constraints、`uv.lock`、`requirements.runtime.txt`，只增删相关包，不动其他固定版本。
- README：
  - 「运行方式」改为常驻代理；
  - 新增 knowledgefs 库说明；
  - 修正 Minor M4（max_concurrent 与数据库槽位的说法）。
- 本地镜像构建：`import botocore` 成功、`/opt/teleport/bin/tsh` 正常；冒烟测试通过。
- 最终全量测试，然后做一次独立审查。

### Task 6: 上线校验（需要用户合并）

按 spec §5 校验，结果补到 spec 并提交。
