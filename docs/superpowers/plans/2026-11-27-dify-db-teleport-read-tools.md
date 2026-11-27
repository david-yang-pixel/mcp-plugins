# dify-db Teleport 只读工具（plugin_lambda_logs、kube_read）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 db-gateway 里新增两个只读 MCP 工具：`plugin_lambda_logs`（查插件 Lambda 的 CloudWatch 日志）和 `kube_read`（只读 kubectl），复用网关已有的 Teleport 会话。

**Architecture:** 新增一个薄的 tsh 子进程执行层（`teleport_cli.py`），负责环境、时间预算、输出上限和按需登录；两个工具各一个模块，在执行层之上做参数校验和结果整理；`mcp_schema.py`/`mcp.py` 按配置把新工具加进 `tools/list` 和分发。不加容器、不加 Secret、不改 watchdog 和隧道容器。

**Tech Stack:** Python 3.12、aiohttp、pydantic、pytest（`asyncio_mode=auto`）、uv；运行时依赖宿主机挂载的 `tsh` v18 和新加入镜像的 aws CLI v1（pip `awscli`）。

**Spec:** `docs/superpowers/specs/2026-11-27-dify-db-teleport-read-tools-design.md`

## Global Constraints

- 只改 `db-gateway/` 目录（文档在仓库根 `docs/`）。不改端口、网络、挂载、Secret、其他服务目录；不重启或修改 `tsh-proxy-*`、`tsh-auth-watchdog`。
- 所有命令在 `db-gateway/` 下执行：`uv sync --locked --all-extras --all-groups`、`uv run --locked pytest -q`。
- 子进程一律用参数列表（`asyncio.create_subprocess_exec`），不经 shell。
- tsh 子进程环境：`TELEPORT_HOME=<cfg.teleport.auth_home>`、`KUBECONFIG=<cfg.teleport_tools.kube.kubeconfig_path>`；**删除** `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及其小写形式。不要像 `teleport_login.py` 那样设 `NO_PROXY=*`：`tsh aws` 靠给 aws 子进程设 `HTTPS_PROXY` 走本地代理，继承到 `NO_PROXY=*` 会让 aws 绕过代理。
- 单次工具调用总耗时必须小于 60 秒（Nginx `/db-gateway/` 路由 `proxy_read_timeout 60s`）。`time_budget_seconds` 默认 40。
- 工具返回都是 dict，带 `success`（bool）、`code`（成功为 `""`）、`error`（成功为 `None`）、`elapsed_ms`，与现有 `execute_schema_lookup` 一致。
- 新工具 schema 的 `annotations` 固定为 `{"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}`。
- `mcp_schema.TOOLS` 保持为现有两个工具不变（stdio 脚本和现有测试依赖它）。
- 审计日志不写日志内容和 kubectl 输出。
- 提交信息结尾按会话要求附 Co-Authored-By 和 Claude-Session 两行。推送、合并到 `main`（即生产部署）需要用户明确授权。

## Review Focus

1. **代理环境变量**：宿主或容器哪天配了 `HTTPS_PROXY`/`NO_PROXY`，`tsh aws` 不应受影响 → Task 2 `test_run_tsh_env`。
2. **续期窗口**：watchdog 清空 `keys/` 的约 10 秒内调用，应返回 `TELEPORT_EXPIRED`（可重试），不能报成「没有权限」→ Task 2 `test_login_failure_without_session_is_expired`。
3. **稀疏过滤的空页**：CloudWatch 可能连续返回 `events=[]` 但带 `nextToken`，应继续翻而不是当成取完 → Task 4 `test_empty_pages_with_token_keep_paging`。
4. **kubectl 短参数连写**：`-Aw`、`-fpods.yaml`、`--as=admin`、`--watch=true` 都要被拒；`-n`/`-o`/`-l` 后面带的值不能误判 → Task 5 `test_blocked_flags`、`test_allowed_flags`。
5. **非法 `tenant_id`**：不是 UUID 时直接 `INVALID_ARGUMENT`，不发 SQL → Task 4 `test_bad_tenant_id_is_rejected_without_query`。

---

## File Structure

| 文件 | 职责 |
| --- | --- |
| `src/db_gateway/config.py`（改） | `TeleportToolsConfig` 及默认值 |
| `src/db_gateway/teleport_cli.py`（新） | `run_tsh`、`run_tsh_with_login`、异常类型 |
| `src/db_gateway/pg_pool.py`（改） | `keys_signature` 跳过 `-app`/`-kube` 目录 |
| `src/db_gateway/lambda_logs.py`（新） | 时间解析、函数名定位、按页取日志、`execute_plugin_lambda_logs` |
| `src/db_gateway/kube_read.py`（新） | 参数校验、context 解析、`execute_kube_read` |
| `src/db_gateway/mcp_schema.py`、`mcp.py`（改） | 两个 schema、`tools_for(cfg)`、分发 |
| `deploy/runtime.json`、`config.example.yaml`、`README.md`（改） | 配置和文档 |
| `pyproject.toml`、`uv.lock`、`requirements.runtime.txt`（改） | aws CLI |
| `tests/services/test_teleport_cli.py`、`test_lambda_logs.py`、`test_kube_read.py`（新） | 单元测试 |

测试里的假 tsh：在 `tmp_path` 写一个可执行的 Python 脚本，把收到的 `argv` 和关心的环境变量追加到一个 JSON 行文件，再按测试预置的「响应队列」文件输出 stdout/stderr/退出码/睡眠秒数；用 `monkeypatch.setattr("db_gateway.teleport_cli.TSH_BIN", str(fake))` 接入。这个夹具放在 `tests/services/conftest.py`，名为 `fake_tsh`，提供 `fake_tsh.push(stdout="", stderr="", code=0, sleep=0)` 和 `fake_tsh.calls`（`[{"argv": [...], "env": {...}}]`）。

测试用配置由 `conftest.py` 的 `cfg` 夹具提供（见 Task 2），审计目录、`auth_home`、kubeconfig 都指向 `tmp_path`。

---

### Task 1: 配置

**Files:**
- Modify: `db-gateway/src/db_gateway/config.py`
- Modify: `db-gateway/deploy/runtime.json`、`db-gateway/config.example.yaml`
- Test: `db-gateway/tests/services/test_config.py`

**Interfaces:**
- Produces: `cfg.teleport_tools`，字段与默认值：
  - `time_budget_seconds: float = 40`、`max_concurrent: int = 3`
  - `lambda_logs`: `enabled: bool = True`、`app: str = "aws-support-logs"`、`aws_role: str = "SupportLambdaLogsReadOnly"`、`region: str = "us-east-1"`、`default_timezone: str = "Asia/Shanghai"`、`default_limit: int = 1000`、`max_limit: int = 10000`
  - `kube`: `enabled: bool = True`、`clusters: dict[str, str] = {"prod": "lg-eks-01", "staging": "dify-plugin-1"}`、`max_output_bytes: int = 1048576`、`kubeconfig_path: str = "/tmp/db-gateway/kubeconfig"`

- [ ] **Step 1: 写失败的测试**

```python
def test_teleport_tools_defaults():
    t = AppConfig().teleport_tools
    assert (t.time_budget_seconds, t.max_concurrent) == (40, 3)
    assert t.lambda_logs.model_dump() == {
        "enabled": True, "app": "aws-support-logs", "aws_role": "SupportLambdaLogsReadOnly",
        "region": "us-east-1", "default_timezone": "Asia/Shanghai",
        "default_limit": 1000, "max_limit": 10000,
    }
    assert t.kube.clusters == {"prod": "lg-eks-01", "staging": "dify-plugin-1"}
    assert t.kube.max_output_bytes == 1048576
    assert t.kube.kubeconfig_path == "/tmp/db-gateway/kubeconfig"


def test_runtime_settings_enable_both_teleport_tools():
    settings = json.loads((ROOT / "deploy" / "runtime.json").read_text())["settings"]
    t = AppConfig(**settings).teleport_tools
    assert t.lambda_logs.enabled is True and t.kube.enabled is True
```

（`ROOT` 沿用该测试文件里已有的 runtime.json 定位方式；没有就用 `Path(__file__).resolve().parents[2]`。）

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --locked pytest tests/services/test_config.py -q`
Expected: FAIL，`AppConfig` 没有 `teleport_tools`

- [ ] **Step 3: 在 `config.py` 加 `LambdaLogsConfig`、`KubeReadConfig`、`TeleportToolsConfig`，并挂到 `AppConfig.teleport_tools`**

- [ ] **Step 4: 在 `deploy/runtime.json` 的 `settings` 里加 `"teleport_tools": {"lambda_logs": {"enabled": true}, "kube": {"enabled": true}}`；`config.example.yaml` 加同名段落并列出全部键和默认值**

- [ ] **Step 5: 运行，确认通过**

Run: `uv run --locked pytest tests/services/test_config.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add db-gateway/src/db_gateway/config.py db-gateway/deploy/runtime.json db-gateway/config.example.yaml db-gateway/tests/services/test_config.py
git commit -m "db-gateway: teleport_tools 配置"
```

---

### Task 2: tsh 执行层

**Files:**
- Create: `db-gateway/src/db_gateway/teleport_cli.py`
- Create: `db-gateway/tests/services/conftest.py`（`fake_tsh` 夹具）
- Test: `db-gateway/tests/services/test_teleport_cli.py`

**Interfaces:**
- Consumes: `cfg.teleport.auth_home`、`cfg.teleport.proxy`、`cfg.teleport.user`、`cfg.teleport_tools.kube.kubeconfig_path`；`db_gateway.postgres.TeleportAuthExpired`；`db_gateway.teleport.get_tsh_status`
- Produces:

```python
TSH_BIN = "tsh"
LOGIN_TIMEOUT_SECONDS = 20.0

@dataclass
class TshResult:
    exit_code: int | None      # 被时间预算杀掉时为 None
    stdout: str                # utf-8，errors="replace"
    stderr: str
    timed_out: bool
    truncated: bool            # stdout 超过 max_output_bytes
    elapsed_ms: int

class TeleportNotAuthorized(Exception): ...

async def run_tsh(args: list[str], *, cfg, timeout: float,
                  max_output_bytes: int | None = None) -> TshResult

async def run_tsh_with_login(args: list[str], *, cfg, timeout: float,
                             login_args: list[str],
                             needs_login: Callable[[TshResult], bool],
                             max_output_bytes: int | None = None) -> TshResult

async def tsh_login(login_args: list[str], *, cfg, not_before: float | None = None) -> None
```

行为约定：
- `run_tsh`：边读边收；stdout 只保留前 `max_output_bytes` 字节但继续读完（不让子进程堵在管道上）；stderr 最多保留 64 KiB；到 `timeout` 就 kill，返回已读到的部分和 `timed_out=True`。
- `run_tsh_with_login`：先执行，`needs_login(result)` 为真才调 `tsh_login(login_args, cfg=cfg, not_before=<本次调用开始的 time.monotonic()>)` 并重试一次（重试的超时取剩余时间，至少 5 秒）。
- `tsh_login`：按 `tuple(login_args)` 取一把模块级 `asyncio.Lock`；拿到锁后，如果 `not_before` 之后已经有一次同参数的登录成功，就直接返回。登录命令 `run_tsh(login_args, timeout=LOGIN_TIMEOUT_SECONDS)`。
- `tsh_login` 失败时调 `get_tsh_status(proxy=…, user=…, auth_home=…)`：返回 `None` → `raise TeleportAuthExpired`；有状态 → `raise TeleportNotAuthorized(f"{stderr 前 600 字符}；当前 Roles: {', '.join(status.roles)}")`。

- [ ] **Step 1: 写 `fake_tsh` 夹具和失败的测试**

```python
async def test_run_tsh_env(cfg, fake_tsh, monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:7890")
    fake_tsh.push(stdout="ok\n")
    r = await run_tsh(["status"], cfg=cfg, timeout=5)
    assert (r.exit_code, r.stdout, r.timed_out, r.truncated) == (0, "ok\n", False, False)
    call = fake_tsh.calls[0]
    assert call["argv"] == ["status"]
    assert call["env"]["TELEPORT_HOME"] == cfg.teleport.auth_home
    assert call["env"]["KUBECONFIG"] == cfg.teleport_tools.kube.kubeconfig_path
    assert not [k for k in call["env"] if k.lower() in
                ("https_proxy", "http_proxy", "all_proxy", "no_proxy")]


async def test_run_tsh_passes_shell_metacharacters_verbatim(cfg, fake_tsh):
    fake_tsh.push()
    await run_tsh(["aws", "--filter-pattern", '"ERROR"; rm -rf / $(x)'], cfg=cfg, timeout=5)
    assert fake_tsh.calls[0]["argv"][2] == '"ERROR"; rm -rf / $(x)'


async def test_run_tsh_caps_stdout_and_decodes_bad_bytes(cfg, fake_tsh):
    fake_tsh.push(stdout_bytes=b"\xff" + b"a" * 5000)
    r = await run_tsh(["x"], cfg=cfg, timeout=5, max_output_bytes=1000)
    assert r.truncated is True and len(r.stdout.encode()) <= 1002 and r.exit_code == 0


async def test_run_tsh_time_budget_returns_partial(cfg, fake_tsh):
    fake_tsh.push(stdout="first\n", sleep=30)   # 先输出并 flush，再睡
    r = await run_tsh(["x"], cfg=cfg, timeout=1)
    assert r.timed_out is True and r.exit_code is None and r.stdout == "first\n"
    assert r.elapsed_ms < 5000


async def test_login_then_retry(cfg, fake_tsh):
    fake_tsh.push(stderr="no creds", code=1)    # 第一次执行
    fake_tsh.push()                              # 登录
    fake_tsh.push(stdout="done")                 # 重试
    r = await run_tsh_with_login(["aws", "x"], cfg=cfg, timeout=10,
                                 login_args=["apps", "login", "a"],
                                 needs_login=lambda res: res.exit_code != 0)
    assert r.stdout == "done"
    assert [c["argv"] for c in fake_tsh.calls] == [["aws", "x"], ["apps", "login", "a"], ["aws", "x"]]


async def test_concurrent_calls_login_once(cfg, fake_tsh):
    fake_tsh.until_login(code=1)   # 登录调用出现之前，其他调用都返回 code=1；之后返回 0
    await asyncio.gather(*[run_tsh_with_login(["aws", str(i)], cfg=cfg, timeout=10,
        login_args=["apps", "login", "a"], needs_login=lambda r: r.exit_code != 0)
        for i in range(5)])
    assert sum(c["argv"][:2] == ["apps", "login"] for c in fake_tsh.calls) == 1


async def test_login_failure_without_session_is_expired(cfg, fake_tsh, monkeypatch):
    monkeypatch.setattr("db_gateway.teleport_cli.get_tsh_status", AsyncMock(return_value=None))
    fake_tsh.push(code=1); fake_tsh.push(stderr="ERROR: Not logged in.", code=1)
    with pytest.raises(TeleportAuthExpired):
        await run_tsh_with_login(["aws"], cfg=cfg, timeout=10, login_args=["apps", "login", "a"],
                                 needs_login=lambda r: True)


async def test_login_failure_with_session_is_not_authorized(cfg, fake_tsh, monkeypatch):
    status = TshStatus(user="u", cluster="c", roles=["production-read-all"],
                       valid_until=datetime.now(timezone.utc), remaining=timedelta(hours=1))
    monkeypatch.setattr("db_gateway.teleport_cli.get_tsh_status", AsyncMock(return_value=status))
    fake_tsh.push(code=1); fake_tsh.push(stderr='app "a" not found', code=1)
    with pytest.raises(TeleportNotAuthorized, match="production-read-all"):
        await run_tsh_with_login(["aws"], cfg=cfg, timeout=10, login_args=["apps", "login", "a"],
                                 needs_login=lambda r: True)
```

`fake_tsh.until_login(code=1, on_login=None)`：让假脚本在收到 `apps login` 或 `kube login` 之前对其他调用返回给定退出码，之后返回 0；`on_login` 可以是一个要写出的文件（路径和内容），供 Task 5 生成 kubeconfig。实现方式由实现者定（例如脚本检查一个标记文件），只要断言成立。

`cfg` 夹具也放在 `conftest.py`：`AppConfig(teleport={"auth_home": str(tmp_path / "tsh"), "proxy": "teleport.dify.dev:443", "user": "u"}, db_gateway={"audit_log_dir": str(tmp_path / "audit")}, teleport_tools={"kube": {"kubeconfig_path": str(tmp_path / "kubeconfig")}})`。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --locked pytest tests/services/test_teleport_cli.py -q`
Expected: FAIL，`db_gateway.teleport_cli` 不存在

- [ ] **Step 3: 实现 `teleport_cli.py`（按 Interfaces 的签名和行为约定）**

- [ ] **Step 4: 运行，确认通过**

Run: `uv run --locked pytest tests/services/test_teleport_cli.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add db-gateway/src/db_gateway/teleport_cli.py db-gateway/tests/services/conftest.py db-gateway/tests/services/test_teleport_cli.py
git commit -m "db-gateway: tsh 只读执行层和按需登录"
```

---

### Task 3: 连接池监视跳过 app / kube 证书目录

**Files:**
- Modify: `db-gateway/src/db_gateway/pg_pool.py`（`keys_signature`，约 104–126 行）
- Test: `db-gateway/tests/services/test_pg_pool_unit.py`

- [ ] **Step 1: 写失败的测试**

```python
def test_keys_signature_ignores_app_and_kube_dirs(tmp_path):
    base = tmp_path / "keys" / "teleport.dify.dev"
    base.mkdir(parents=True)
    (base / "yangyonggang.crt").write_text("x")
    before = keys_signature(str(tmp_path))
    for sub in ("yangyonggang-app/teleport.dify.dev", "yangyonggang-kube/teleport.dify.dev"):
        (base / sub).mkdir(parents=True)
        (base / sub / "aws-support-logs.crt").write_text("y")
    assert keys_signature(str(tmp_path)) == before
    (base / "yangyonggang-db").mkdir()
    (base / "yangyonggang-db" / "lg-billing-01.crt").write_text("z")
    assert keys_signature(str(tmp_path)) != before
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --locked pytest tests/services/test_pg_pool_unit.py -q`
Expected: 新测试 FAIL，其余 PASS

- [ ] **Step 3: 在 `keys_signature` 的 `os.walk` 里原地从 `_dirs` 去掉名称以 `-app` 或 `-kube` 结尾的目录；更新 docstring 说明原因**

- [ ] **Step 4: 运行，确认通过**

Run: `uv run --locked pytest tests/services/test_pg_pool_unit.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add db-gateway/src/db_gateway/pg_pool.py db-gateway/tests/services/test_pg_pool_unit.py
git commit -m "db-gateway: 连接池监视忽略 app/kube 证书目录"
```

---

### Task 4: plugin_lambda_logs

**Files:**
- Create: `db-gateway/src/db_gateway/lambda_logs.py`
- Test: `db-gateway/tests/services/test_lambda_logs.py`

**Interfaces:**
- Consumes: Task 1 配置；Task 2 `run_tsh_with_login`、`TshResult`、`TeleportNotAuthorized`；`db_gateway.core` 的 `_db_target`、`_metadata_kwargs`、`_query_limiter`、`_audit_path`；`db_gateway.pg_pool.run_metadata_query`；`db_gateway.postgres` 的 `TeleportAuthExpired`、`audit_log`
- Produces:

```python
def parse_time_ms(value: Any, *, default_timezone: str) -> int      # 失败抛 ValueError
async def execute_plugin_lambda_logs(*, cfg, function_name=None, tenant_id=None, plugin_id=None,
        start_time=None, end_time=None, since_minutes=None, filter_pattern=None,
        log_stream_names=None, limit=None, next_token=None) -> dict[str, Any]
```

成功返回的键：`success`、`code`、`error`、`elapsed_ms`、`function_name`、`log_group`、`region`、`plugin_unique_identifier`（仅按工作空间定位时）、`window`（`{"start_utc", "end_utc", "start_local", "end_local", "timezone"}`，ISO 字符串，`end` 为不含端点）、`events`（`[{"timestamp", "log_stream", "event_id", "message"}]`，`timestamp` 为 UTC ISO 毫秒精度）、`event_count`、`next_token`、`complete`、`warnings`。

决定：
- `parse_time_ms`：`int`/`float` 或纯数字字符串按毫秒时间戳；其余用 `datetime.fromisoformat`（先把结尾的 `Z` 换成 `+00:00`）；无时区的用 `ZoneInfo(default_timezone)`。
- 定位：`function_name` 与 `tenant_id`+`plugin_id` 必须恰好给一种，否则 `INVALID_ARGUMENT`。`function_name` 须匹配 `^[A-Za-z0-9_-]{1,64}$`。`tenant_id` 用 `uuid.UUID()` 校验；`plugin_id` 须含恰好一个 `/`。
- 定位 SQL（`plugin` 库，绑定参数 `(tenant_id, plugin_id)`，超时 15 秒，套 `_query_limiter("plugin", port, cfg.postgres.max_concurrent_queries)`）：

```sql
SELECT i.plugin_unique_identifier, i.runtime_type, r.function_name, r.function_url
FROM public.plugin_installations AS i
LEFT JOIN public.serverless_runtimes AS r
  ON r.plugin_unique_identifier = i.plugin_unique_identifier
WHERE i.tenant_id = %s::uuid AND i.plugin_id = %s
```

  无行 → `UNKNOWN_FUNCTION`「该工作空间没有安装 {plugin_id}」；`function_name` 为空 → `UNKNOWN_FUNCTION`「有安装记录（runtime_type={…}）但没有运行映射」；`function_url` 不匹配 `^https://[^/]+\.lambda-url\.{region}\.on\.aws(/|$)` → `UNKNOWN_FUNCTION`「运行后端不是 Lambda {region}」。数据库的 `TeleportAuthExpired` → `TELEPORT_EXPIRED`，其他异常 → `QUERY_ERROR`。按工作空间定位成功时 `warnings` 加一条：「安装表反映当前状态；故障后升级或重装过，请按故障时的版本用 db_gateway_query 查 serverless_runtimes。」
- 窗口：`since_minutes`（正整数）与 `start_time`+`end_time` 恰好给一种；`end > start`。传给 CloudWatch 的是 `--start-time start_ms`、`--end-time end_ms - 1`。
- `limit`：缺省 `default_limit`；大于 `max_limit` 时取 `max_limit` 并加 warning；小于 1 → `INVALID_ARGUMENT`。
- 每页命令（`remaining = limit - 已取条数`）：

```
aws --app <app> logs filter-log-events --region <region>
    --log-group-name /aws/lambda/<fn> --start-time <ms> --end-time <ms>
    [--filter-pattern <p>] [--log-stream-names <a> <b> …]
    --limit <min(remaining, 10000)> --no-paginate [--next-token <t>] --output json
```

  （aws CLI v1 没有 `--no-cli-pager`，不要加。）`login_args = ["apps", "login", "--aws-role", <aws_role>, <app>]`；`needs_login = lambda r: r.exit_code not in (0, None) and "An error occurred (" not in r.stderr`。
- 翻页循环：每页超时 = `time_budget_seconds - 已用`（至少 5 秒）。一页成功后把 `events` 追加、记下 `nextToken`。停止条件按顺序：没有 `nextToken` → `complete=True, next_token=None`；已取条数 ≥ `limit` → 返回 `nextToken`，warning「已达到 limit」；已用时间 ≥ `time_budget_seconds * 0.6` → 返回 `nextToken`，warning「时间预算用完，用 next_token 继续」。
- 某页失败：
  - `timed_out` 且是第一页 → `success=False`、`code="LOGS_TIMEOUT"`，`error` 提示缩小窗口或加 `filter_pattern`。
  - 非零退出且是第一页 → `success=False`、`code="AWS_ERROR"`，`error` = stderr 前 800 字符。
  - 之前已有成功的页（不论超时还是非零退出）→ `success=True`，返回已取到的事件和上一页的 `nextToken`，`complete=False`，加一条 warning 说明中断原因。
  - `TeleportAuthExpired` → `TELEPORT_EXPIRED`；`TeleportNotAuthorized` → `NOT_AUTHORIZED`，`error` 后附「需要 Teleport 角色 support-lambda-logs」。
- `complete=True` 且 `event_count == 0` 时加 warning：「没有匹配的日志：先去掉 filter_pattern、把窗口放宽到故障前后各 5 分钟，再核对时区和该工作空间实际安装的 checksum；同一函数被多个工作空间共用，关联客户请求需要时间或 RequestId。」
- 并发：模块级 `asyncio.Semaphore(cfg.teleport_tools.max_concurrent)`，按 `max_concurrent` 值惰性创建。
- 审计：`audit_log(log_path=_audit_path(cfg), database="", sql="", status=<"OK" 或 code>, row_count=event_count, elapsed_ms=…, error=…, tool="plugin_lambda_logs", function_name=…, log_group=…, start_ms=…, end_ms=…, filter_pattern=…, limit=…, has_next_token=bool(next_token 入参), complete=…)`。

- [ ] **Step 1: 写失败的测试**

辅助：`page(events, token=None)` 返回 `json.dumps({"events": [...], **({"nextToken": token} if token else {})})`，事件形如 `{"timestamp": 1795775460000, "logStreamName": "s", "eventId": "1", "message": "m"}`。

```python
@pytest.mark.parametrize("value,expected", [
    ("2026-11-27T18:31:00+08:00", 1795775460000),
    ("2026-11-27 18:31:00", 1795775460000),          # 无时区按北京时间
    ("2026-11-27T10:31:00Z", 1795775460000),
    (1795775460000, 1795775460000),
    ("1795775460000", 1795775460000),
])
def test_parse_time_ms(value, expected):
    assert parse_time_ms(value, default_timezone="Asia/Shanghai") == expected


async def test_single_page_by_function_name(cfg, fake_tsh):
    fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="9a442b2d0d9b43e56a190df6ca7dc207",
        start_time="2026-11-27 18:31:00", end_time="2026-11-27 18:32:00", filter_pattern='"ERROR"')
    assert out["success"] and out["complete"] is True and out["next_token"] is None
    assert out["log_group"] == "/aws/lambda/9a442b2d0d9b43e56a190df6ca7dc207"
    assert out["events"] == [{"timestamp": "2026-11-27T10:31:00.000Z", "log_stream": "s",
                              "event_id": "1", "message": "m"}]
    assert out["window"]["start_local"].startswith("2026-11-27T18:31:00")
    argv = fake_tsh.calls[0]["argv"]
    assert argv[:5] == ["aws", "--app", "aws-support-logs", "logs", "filter-log-events"]
    assert argv[argv.index("--start-time") + 1] == "1795775460000"
    assert argv[argv.index("--end-time") + 1] == "1795775519999"
    assert argv[argv.index("--filter-pattern") + 1] == '"ERROR"'
    assert argv[argv.index("--limit") + 1] == "1000"
    assert "--no-paginate" in argv and "--no-cli-pager" not in argv


async def test_long_message_is_not_truncated(cfg, fake_tsh):
    fake_tsh.push(stdout=page([{**EV, "message": "x" * 300_000}]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert len(out["events"][0]["message"]) == 300_000


async def test_limit_stops_with_token(cfg, fake_tsh):
    fake_tsh.push(stdout=page([EV, EV], token="T1"))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10, limit=2)
    assert (out["event_count"], out["next_token"], out["complete"]) == (2, "T1", False)
    assert len(fake_tsh.calls) == 1


async def test_empty_pages_with_token_keep_paging(cfg, fake_tsh):
    fake_tsh.push(stdout=page([], token="T1")); fake_tsh.push(stdout=page([], token="T2"))
    fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert (out["event_count"], out["complete"]) == (1, True)
    assert fake_tsh.calls[1]["argv"][fake_tsh.calls[1]["argv"].index("--next-token") + 1] == "T1"


async def test_time_budget_returns_partial_with_token(cfg, fake_tsh):
    cfg.teleport_tools.time_budget_seconds = 2
    fake_tsh.push(stdout=page([EV], token="T1"), sleep=1.5); fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"] and (out["event_count"], out["next_token"], out["complete"]) == (1, "T1", False)


async def test_first_page_timeout_is_logs_timeout(cfg, fake_tsh):
    cfg.teleport_tools.time_budget_seconds = 1
    fake_tsh.push(sleep=30)
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert (out["success"], out["code"]) == (False, "LOGS_TIMEOUT")


async def test_aws_error_is_reported_without_login(cfg, fake_tsh):
    fake_tsh.push(code=254, stderr="An error occurred (ResourceNotFoundException) when calling "
                  "the FilterLogEvents operation: The specified log group does not exist.")
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["code"] == "AWS_ERROR" and "ResourceNotFoundException" in out["error"]
    assert len(fake_tsh.calls) == 1


async def test_missing_app_cert_logs_in_and_retries(cfg, fake_tsh):
    fake_tsh.push(code=1, stderr="some tsh error"); fake_tsh.push(); fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"]
    assert fake_tsh.calls[1]["argv"] == ["apps", "login", "--aws-role", "SupportLambdaLogsReadOnly",
                                         "aws-support-logs"]


async def test_resolve_by_tenant_and_plugin(cfg, fake_tsh, monkeypatch):
    q = AsyncMock(return_value=[["langgenius/openai:1.2.1@abc", "serverless", "fn--2",
                                 "https://x.lambda-url.us-east-1.on.aws/"]])
    monkeypatch.setattr("db_gateway.lambda_logs.run_metadata_query", q)
    fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, tenant_id="6f1d2c3e-0000-4000-8000-000000000001",
                                           plugin_id="langgenius/openai", since_minutes=10)
    assert out["function_name"] == "fn--2"
    assert out["plugin_unique_identifier"] == "langgenius/openai:1.2.1@abc"
    assert q.call_args.kwargs["params"] == ("6f1d2c3e-0000-4000-8000-000000000001", "langgenius/openai")
    assert any("安装表反映当前状态" in w for w in out["warnings"])


@pytest.mark.parametrize("rows,needle", [
    ([], "没有安装"),
    ([["p:1@c", "local", None, None]], "没有运行映射"),
    ([["p:1@c", "serverless", "fn", "https://example.com/"]], "不是 Lambda"),
])
async def test_resolve_failures(cfg, fake_tsh, monkeypatch, rows, needle):
    monkeypatch.setattr("db_gateway.lambda_logs.run_metadata_query", AsyncMock(return_value=rows))
    out = await execute_plugin_lambda_logs(cfg=cfg, tenant_id="6f1d2c3e-0000-4000-8000-000000000001",
                                           plugin_id="a/b", since_minutes=10)
    assert out["code"] == "UNKNOWN_FUNCTION" and needle in out["error"] and fake_tsh.calls == []


async def test_bad_tenant_id_is_rejected_without_query(cfg, fake_tsh, monkeypatch):
    q = AsyncMock(); monkeypatch.setattr("db_gateway.lambda_logs.run_metadata_query", q)
    out = await execute_plugin_lambda_logs(cfg=cfg, tenant_id="not-a-uuid", plugin_id="a/b", since_minutes=5)
    assert out["code"] == "INVALID_ARGUMENT" and q.await_count == 0


@pytest.mark.parametrize("kwargs", [
    {},                                                           # 没给定位
    {"function_name": "f", "tenant_id": "x", "plugin_id": "a/b"}, # 两种都给
    {"function_name": "../etc"},                                  # 非法函数名
    {"function_name": "f"},                                       # 没给窗口
    {"function_name": "f", "since_minutes": 5, "start_time": 1, "end_time": 2},
    {"function_name": "f", "start_time": 2000, "end_time": 1000},
    {"function_name": "f", "since_minutes": 0},
    {"function_name": "f", "since_minutes": 5, "limit": 0},
    {"function_name": "f", "start_time": "昨天", "end_time": "今天"},
])
async def test_invalid_arguments(cfg, fake_tsh, kwargs):
    out = await execute_plugin_lambda_logs(cfg=cfg, **kwargs)
    assert out["code"] == "INVALID_ARGUMENT" and fake_tsh.calls == []


async def test_limit_above_max_is_clamped_with_warning(cfg, fake_tsh):
    fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5, limit=99999)
    assert fake_tsh.calls[0]["argv"][fake_tsh.calls[0]["argv"].index("--limit") + 1] == "10000"
    assert out["warnings"]


async def test_audit_has_no_log_content(cfg, fake_tsh, tmp_path):
    cfg.db_gateway.audit_log_dir = str(tmp_path / "audit")
    fake_tsh.push(stdout=page([{**EV, "message": "SECRET-PAYLOAD"}]))
    await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    text = (tmp_path / "audit" / "db_gateway_audit.log").read_text()
    assert '"tool": "plugin_lambda_logs"' in text and "SECRET-PAYLOAD" not in text
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --locked pytest tests/services/test_lambda_logs.py -q`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 `lambda_logs.py`（按 Interfaces 和「决定」）**

- [ ] **Step 4: 运行，确认通过**

Run: `uv run --locked pytest tests/services/test_lambda_logs.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add db-gateway/src/db_gateway/lambda_logs.py db-gateway/tests/services/test_lambda_logs.py
git commit -m "db-gateway: plugin_lambda_logs 查插件 Lambda 日志"
```

---

### Task 5: kube_read

**Files:**
- Create: `db-gateway/src/db_gateway/kube_read.py`
- Test: `db-gateway/tests/services/test_kube_read.py`

**Interfaces:**
- Consumes: Task 1 配置；Task 2 `run_tsh_with_login`、`tsh_login`、`TshResult`、`TeleportNotAuthorized`；`db_gateway.core._audit_path`；`db_gateway.postgres` 的 `TeleportAuthExpired`、`audit_log`
- Produces:

```python
def validate_kubectl_args(args: Any) -> list[str]            # 不合法抛 ValueError（中文原因）
def resolve_context(kubeconfig_path: str, *, proxy: str, cluster_name: str) -> str | None
async def execute_kube_read(*, cfg, cluster: Any, args: Any) -> dict[str, Any]
```

成功返回的键：`success`、`code`、`error`、`elapsed_ms`、`cluster`、`context`、`output`、`stderr`、`exit_code`、`truncated`、`timed_out`、`warnings`。

决定：
- `validate_kubectl_args`：
  - 必须是非空的字符串列表，最多 64 项，单项最长 4096 字符，不含 `\x00`。
  - `args[0]` 属于 `{"get", "describe", "logs", "top", "explain", "api-resources", "api-versions", "version", "auth"}`；`auth` 时 `args[1]` 必须是 `can-i`。
  - 只检查第一个 `--` 之前的参数。
  - 长参数：参数等于下列之一，或以「下列之一 + `=`」开头，就拒绝：`--filename --kustomize --kubeconfig --context --cluster --user --as --as-group --as-uid --token --server --certificate-authority --client-certificate --client-key --insecure-skip-tls-verify --tls-server-name --username --password --follow --watch --watch-only --cache-dir --profile --profile-output`。
  - 短参数（以单个 `-` 开头、长度 ≥ 2、不以 `--` 开头）：从第二个字符起逐个看；遇到 `n o l c L` 之一就停（后面是它的值）；在停之前遇到 `f k w s` 之一就拒绝。
  - 错误信息说明是哪一个参数、属于哪一类（读本地文件 / 换身份或目标 / 持续输出 / 写本地文件）。
- `resolve_context`：用 `yaml.safe_load` 读 kubeconfig，`host = proxy.split(":")[0]`；在 `contexts[].name` 里找等于 `f"{host}-{cluster_name}"` 或以 `f"{host}-{cluster_name}-"` 开头的，恰好一个则返回它，否则（文件不存在、解析失败、0 个或多个）返回 `None`。
- `execute_kube_read`：
  - `cluster` 不在 `cfg.teleport_tools.kube.clusters` → `INVALID_ARGUMENT`，`error` 列出可用别名。参数校验失败 → `INVALID_ARGUMENT`。
  - `login_args = ["kube", "login", "--all"]`。先 `resolve_context`；为 `None` 时调 `tsh_login(login_args, cfg=cfg)` 一次，再 `resolve_context`，仍为 `None` → `NOT_AUTHORIZED`「Teleport 里看不到集群 {cluster_name}」。
  - 命令：`["kubectl", args[0], "--context", <context>, *args[1:]]`（`--context` 放在子命令之后：放在前面会被 tsh 当成自己的参数）。
  - `needs_login = lambda r: r.exit_code not in (0, None) and any(m in r.stderr for m in KUBE_LOGIN_MARKERS)`，`KUBE_LOGIN_MARKERS = ("getting credentials", "tsh kube login", "exec plugin", "context was not found")`（模块常量，Task 8 用线上实际输出校正）。
  - `timeout=cfg.teleport_tools.time_budget_seconds`，`max_output_bytes=cfg.teleport_tools.kube.max_output_bytes`。
  - 结果：`exit_code == 0` → 成功；`timed_out` → `success=True`、warning「到时间预算被中止，输出不完整；用 --tail、--since、-l 缩小范围」；其他非零 → `success=False`、`code="KUBECTL_ERROR"`、`error`=stderr 前 2000 字符，`output` 照常返回。`truncated` 时加 warning「输出超过 {max_output_bytes} 字节，只保留开头；用 --tail、--since、--since-time、-l 缩小范围」。
  - `TeleportAuthExpired` → `TELEPORT_EXPIRED`；`TeleportNotAuthorized` → `NOT_AUTHORIZED`。
  - 并发：模块级信号量，同 Task 4。
  - 审计：`tool="kube_read"`、`cluster`、`args`（完整列表）、`exit_code`、`output_bytes`、`truncated`、`timed_out`；`row_count=0`；不写输出。

- [ ] **Step 1: 写失败的测试**

辅助：`write_kubeconfig(path, names)` 写一个含这些 context 名的最小 kubeconfig；`PROD = "teleport.dify.dev-lg-eks-01-us-east-1-523286616386"`；`cfg.teleport.proxy = "teleport.dify.dev:443"`。

```python
@pytest.mark.parametrize("args", [
    ["get", "pods", "-n", "dify-plugin", "-o", "wide"],
    ["get", "pods", "-A", "-l", "app=dify-api-oltp", "--no-headers"],
    ["get", "pods", "-nfoo", "-owide"],              # 值紧跟短参数
    ["get", "pods", "-n", "kube-system", "-L", "app"],
    ["describe", "pod", "x", "-n", "dify-plugin"],
    ["logs", "deploy/dify-api-oltp", "-n", "dify-plugin", "--since=10m", "--tail=500", "-c", "api", "-p"],
    ["auth", "can-i", "get", "pods", "-n", "dify"],
    ["get", "pods", "--", "-f"],                      # -- 之后不检查
    ["version"], ["api-resources"], ["explain", "deployment.spec"],
])
def test_allowed_flags(args):
    assert validate_kubectl_args(args) == args


@pytest.mark.parametrize("args", [
    ["delete", "pod", "x"], ["exec", "x", "--", "sh"], ["apply", "-f", "x.yaml"],
    ["patch", "deploy", "x"], ["port-forward", "x", "80"], ["cp", "a", "b"], ["edit", "deploy", "x"],
    ["auth", "reconcile"], ["config", "view"], ["-n", "x", "get", "pods"],
    ["get", "-f", "/app/.env"], ["get", "-f/app/.env"], ["get", "--filename=/app/.env"],
    ["get", "-k", "."], ["logs", "x", "-f"], ["logs", "x", "--follow"], ["logs", "x", "--follow=true"],
    ["get", "pods", "-w"], ["get", "pods", "-Aw"], ["get", "pods", "--watch=true"], ["get", "pods", "--watch-only"],
    ["get", "pods", "--as=admin"], ["get", "pods", "--as-group", "system:masters"],
    ["get", "pods", "--context", "other"], ["get", "pods", "--kubeconfig=/tmp/x"],
    ["get", "pods", "--server=https://evil"], ["get", "pods", "-s", "https://evil"],
    ["get", "pods", "--token=abc"], ["get", "pods", "--cache-dir=/tmp/x"],
    ["get", "pods", "--profile=cpu", "--profile-output=/app/x"],
    [], "get pods", ["get", 1], ["get"] * 65, ["get", "a\x00b"],
])
def test_blocked_flags(args):
    with pytest.raises(ValueError):
        validate_kubectl_args(args)


def test_resolve_context(tmp_path):
    kc = tmp_path / "kubeconfig"
    assert resolve_context(str(kc), proxy="teleport.dify.dev:443", cluster_name="lg-eks-01") is None
    write_kubeconfig(kc, [PROD, "teleport.dify.dev-dify-plugin-1-eks-us-east-1-014498624057"])
    assert resolve_context(str(kc), proxy="teleport.dify.dev:443", cluster_name="lg-eks-01") == PROD
    assert resolve_context(str(kc), proxy="teleport.dify.dev:443", cluster_name="nope") is None
    write_kubeconfig(kc, [PROD, PROD + "-copy"])
    assert resolve_context(str(kc), proxy="teleport.dify.dev:443", cluster_name="lg-eks-01") is None


async def test_kube_read_inserts_context_after_verb(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(stdout="NAME READY\n")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods", "-n", "dify-plugin"])
    assert out["success"] and out["output"] == "NAME READY\n" and out["context"] == PROD
    assert fake_tsh.calls[0]["argv"] == ["kubectl", "get", "--context", PROD, "pods", "-n", "dify-plugin"]


async def test_kube_read_logs_in_when_kubeconfig_missing(cfg, fake_tsh, tmp_path):
    fake_tsh.until_login(code=0, on_login=(cfg.teleport_tools.kube.kubeconfig_path, KUBECONFIG_TEXT))
    fake_tsh.push(stdout="ok")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
    assert out["success"]
    assert ["kube", "login", "--all"] in [c["argv"] for c in fake_tsh.calls]


async def test_kube_read_relogs_in_on_credential_error(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(code=1, stderr="Unable to connect to the server: getting credentials: exec: ...")
    fake_tsh.push(); fake_tsh.push(stdout="ok")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
    assert out["output"] == "ok" and len(fake_tsh.calls) == 3


async def test_kube_read_forbidden_is_kubectl_error_without_login(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(code=1, stderr='Error from server (Forbidden): secrets is forbidden: User "u" cannot list')
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "secrets", "-n", "dify-plugin"])
    assert (out["success"], out["code"], out["exit_code"]) == (False, "KUBECTL_ERROR", 1)
    assert "Forbidden" in out["error"] and len(fake_tsh.calls) == 1


async def test_kube_read_rejects_before_running(cfg, fake_tsh, kubeconfig):
    for cluster, args in (("prod", ["delete", "pod", "x"]), ("nope", ["get", "pods"])):
        out = await execute_kube_read(cfg=cfg, cluster=cluster, args=args)
        assert out["code"] == "INVALID_ARGUMENT"
    assert fake_tsh.calls == []


async def test_kube_read_truncates_and_flags(cfg, fake_tsh, kubeconfig):
    cfg.teleport_tools.kube.max_output_bytes = 100
    fake_tsh.push(stdout="x" * 5000)
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "p"])
    assert out["success"] and out["truncated"] is True and len(out["output"]) == 100 and out["warnings"]


async def test_kube_read_timeout_returns_partial(cfg, fake_tsh, kubeconfig):
    cfg.teleport_tools.time_budget_seconds = 1
    fake_tsh.push(stdout="line1\n", sleep=30)
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "p"])
    assert out["success"] and out["timed_out"] is True and out["output"] == "line1\n"


async def test_kube_read_audit_has_args_but_no_output(cfg, fake_tsh, kubeconfig, tmp_path):
    cfg.db_gateway.audit_log_dir = str(tmp_path / "audit")
    fake_tsh.push(stdout="CUSTOMER-DATA")
    await execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "p", "-n", "dify-plugin"])
    text = (tmp_path / "audit" / "db_gateway_audit.log").read_text()
    assert '"tool": "kube_read"' in text and '"dify-plugin"' in text and "CUSTOMER-DATA" not in text
```

`kubeconfig` 夹具：在 `cfg.teleport_tools.kube.kubeconfig_path` 写入含两个 context 的文件。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --locked pytest tests/services/test_kube_read.py -q`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 `kube_read.py`（按 Interfaces 和「决定」）**

- [ ] **Step 4: 运行，确认通过**

Run: `uv run --locked pytest tests/services/test_kube_read.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add db-gateway/src/db_gateway/kube_read.py db-gateway/tests/services/test_kube_read.py
git commit -m "db-gateway: kube_read 只读 kubectl"
```

---

### Task 6: MCP 接入和文档

**Files:**
- Modify: `db-gateway/src/db_gateway/mcp_schema.py`、`db-gateway/src/db_gateway/mcp.py`
- Modify: `db-gateway/README.md`
- Test: `db-gateway/tests/services/test_mcp_http.py`

**Interfaces:**
- Consumes: Task 4 `execute_plugin_lambda_logs`、Task 5 `execute_kube_read`（关键字参数名即工具参数名）
- Produces:

```python
LAMBDA_LOGS_TOOL_SCHEMA: dict[str, Any]     # name="plugin_lambda_logs"
KUBE_READ_TOOL_SCHEMA: dict[str, Any]       # name="kube_read"
def tools_for(cfg: Any) -> list[dict[str, Any]]
```

决定：
- `tools_for(cfg)`：`TOOLS` 的两项，加上 `getattr(cfg.teleport_tools.lambda_logs, "enabled", False) is True` 时的日志工具，加上 kube 同理（用 `is True`，这样现有测试里的 `MagicMock` 配置不会意外开启）。kube 的 schema 深拷贝后把 `inputSchema.properties.cluster.enum` 设为 `list(cfg.teleport_tools.kube.clusters)`，并在 `cluster` 的 description 里写出「别名=Teleport 集群名」。
- `mcp.py`：`tools/list` 用 `tools_for(cfg)`；`tools/call` 对未启用的工具返回 `-32601 unknown tool`；启用时把 `arguments` 里的已知键原样传给对应函数（未知键忽略）。
- `plugin_lambda_logs` 的 `inputSchema`：`function_name`、`tenant_id`、`plugin_id`、`start_time`（`["string","number"]`）、`end_time`、`since_minutes`（integer，minimum 1）、`filter_pattern`、`log_stream_names`（string 数组）、`limit`（integer，minimum 1）、`next_token`；`required` 为空。
- `plugin_lambda_logs` 的 description 要点（英文，与现有工具一致）：查 SaaS 插件 Lambda 的 CloudWatch 日志（us-east-1，`/aws/lambda/<function_name>`）；定位二选一；时间不带时区按 Asia/Shanghai；`complete=false` 时用 `next_token` 和相同的其他参数继续；事件多、消息长会占很多上下文，按需调小 `limit` 或加 `filter_pattern`；`REPORT` 行只是调用统计，查报错用 `"ERROR"` 或 `"Traceback"`，已知 RequestId 时用 `"<id>"`；同一函数被多个工作空间共用；要看某插件全部版本和 checksum，用 `db_gateway_query`（`database=plugin`）跑：

```sql
SELECT r.plugin_unique_identifier, r.function_name, r.updated_at
FROM public.serverless_runtimes AS r
WHERE split_part(r.plugin_unique_identifier, '@', 1) = 'langgenius/openai:1.2.1'
ORDER BY r.updated_at DESC
```

- `kube_read` 的 `inputSchema`：`cluster`（enum）、`args`（string 数组，minItems 1）；两者 required。description 要点：只读 kubectl；允许的子命令列表；`--context` 由网关加，不要传；不支持 `-f/--follow`、`-w/--watch`；当前 Teleport 角色能读 `pods`、`pods/log`、`deployments`，其他资源会被 Teleport 拒绝；生产 SaaS 在 `prod` 集群的 `dify-plugin` 命名空间，staging 在 `staging` 集群的 `dify-staging`；输出上限 1 MB，日志用 `--tail`、`--since`、`--since-time` 控制范围；给 4 个示例（spec §5.4 的那 4 条）。
- README：新增「Teleport 只读工具」一节，写两个工具的参数表、返回字段、错误码（`UNKNOWN_FUNCTION`、`NOT_AUTHORIZED`、`AWS_ERROR`、`LOGS_TIMEOUT`、`KUBECTL_ERROR`）、配置表、按需登录和续期后的行为、`kube_read` 的拒绝规则、「新工具只走 /mcp，没有 HTTP 端点和 stdio 转发」。

- [ ] **Step 1: 写失败的测试**

夹具 `teleport_mcp_client`：与现有 `mcp_client` 相同，但 `cfg.teleport_tools = AppConfig().teleport_tools`（真实对象）。

```python
async def test_tools_list_adds_teleport_tools(teleport_mcp_client):
    data = await (await _post_mcp(teleport_mcp_client, LIST)).json()
    tools = {t["name"]: t for t in data["result"]["tools"]}
    assert list(tools) == ["db_gateway_query", "db_gateway_schema", "plugin_lambda_logs", "kube_read"]
    for name in ("plugin_lambda_logs", "kube_read"):
        assert tools[name]["annotations"] == {"readOnlyHint": True, "destructiveHint": False,
                                              "idempotentHint": True, "openWorldHint": True}
    assert tools["kube_read"]["inputSchema"]["properties"]["cluster"]["enum"] == ["prod", "staging"]


async def test_tools_list_hides_disabled_tools(mcp_client_factory):
    cfg_tools = AppConfig(teleport_tools={"lambda_logs": {"enabled": False},
                                          "kube": {"enabled": False}}).teleport_tools
    async with mcp_client_factory(cfg_tools) as client:
        names = [t["name"] for t in (await (await _post_mcp(client, LIST)).json())["result"]["tools"]]
        assert names == ["db_gateway_query", "db_gateway_schema"]
        resp = await _post_mcp(client, call("kube_read", {"cluster": "prod", "args": ["get", "pods"]}))
        assert (await resp.json())["error"]["code"] == -32601


async def test_existing_mock_config_still_lists_two_tools(mcp_client):
    names = [t["name"] for t in (await (await _post_mcp(mcp_client, LIST)).json())["result"]["tools"]]
    assert names == ["db_gateway_query", "db_gateway_schema"]


async def test_call_plugin_lambda_logs_passes_arguments(teleport_mcp_client):
    payload = {"success": True, "code": "", "error": None, "events": []}
    with patch("db_gateway.mcp.execute_plugin_lambda_logs", AsyncMock(return_value=payload)) as fn:
        resp = await _post_mcp(teleport_mcp_client, call("plugin_lambda_logs",
            {"function_name": "f", "since_minutes": 10, "filter_pattern": '"ERROR"', "ticket_id": "1"}))
    result = (await resp.json())["result"]
    assert result["isError"] is False and json.loads(result["content"][0]["text"]) == payload
    assert fn.call_args.kwargs["function_name"] == "f" and "ticket_id" not in fn.call_args.kwargs


async def test_call_kube_read_error_sets_is_error(teleport_mcp_client):
    payload = {"success": False, "code": "INVALID_ARGUMENT", "error": "x"}
    with patch("db_gateway.mcp.execute_kube_read", AsyncMock(return_value=payload)) as fn:
        resp = await _post_mcp(teleport_mcp_client, call("kube_read", {"cluster": "prod", "args": ["delete"]}))
    assert (await resp.json())["result"]["isError"] is True
    assert fn.call_args.kwargs == {"cfg": ANY, "cluster": "prod", "args": ["delete"]}
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --locked pytest tests/services/test_mcp_http.py -q`
Expected: 新测试 FAIL，其余 PASS

- [ ] **Step 3: 实现 schema、`tools_for`、`mcp.py` 分发**

- [ ] **Step 4: 写 README 新章节**

- [ ] **Step 5: 运行全部测试**

Run: `uv run --locked pytest -q`
Expected: 全部 PASS（含 `tests/scripts/test_db_gateway_mcp_stdio.py`，证明 stdio 未受影响）

- [ ] **Step 6: 提交**

```bash
git add db-gateway/src/db_gateway/mcp_schema.py db-gateway/src/db_gateway/mcp.py db-gateway/README.md db-gateway/tests/services/test_mcp_http.py
git commit -m "db-gateway: 在 MCP 暴露 plugin_lambda_logs 和 kube_read"
```

---

### Task 7: 镜像加入 aws CLI

**Files:**
- Modify: `db-gateway/pyproject.toml`、`db-gateway/uv.lock`、`db-gateway/requirements.runtime.txt`

- [ ] **Step 1: 加依赖并锁定**

Run: `uv add awscli`
把解析出的 `awscli` 及其新增传递依赖（`botocore`、`s3transfer`、`jmespath`、`python-dateutil`、`six`、`urllib3`、`docutils`、`rsa`、`pyasn1`、`colorama` 等，以 `uv.lock` 的 diff 为准）的精确版本，写进 `pyproject.toml` 的 `[tool.uv] constraint-dependencies` 和 `requirements.runtime.txt`（保持字母序）。现有固定版本（如 `pyyaml==6.0.3`）不许变；解析器要改它们时停下来报告。

- [ ] **Step 2: 确认锁定安装和测试**

Run: `uv sync --locked --all-extras --all-groups && uv run --locked pytest -q && uv run --locked aws --version`
Expected: 测试 PASS；输出 `aws-cli/1.x …`

- [ ] **Step 3: 构建镜像并确认 CLI 在 PATH 上**

Run: `docker build -t db-gateway:local . && docker run --rm db-gateway:local aws --version`
Expected: 输出 `aws-cli/1.x …`

- [ ] **Step 4: 镜像冒烟**

Run（仓库根）：`python3 deploy/smoke_image.py db-gateway`（参数形式以脚本为准）
Expected: health、tools/list、错误密钥拒绝通过；tools/list 数量为 4

- [ ] **Step 5: 提交**

```bash
git add db-gateway/pyproject.toml db-gateway/uv.lock db-gateway/requirements.runtime.txt
git commit -m "db-gateway: 镜像加入 aws CLI"
```

---

### Task 8: 上线与线上校验（需要用户授权）

推送分支、开 PR、合并到 `main` 都要用户明确同意。合并后 CI 只应选中 `db-gateway`。

- [ ] **Step 1: 推送前核对**：`git diff --name-only main...` 只含 `db-gateway/` 和 `docs/`。

- [ ] **Step 2: PR 的 CI 通过后，核对 `plan` 作业只选中 db-gateway**；否则停下来定位。

- [ ] **Step 3: 部署前记录**（首尔服务器）：`docker ps --format '{{.Names}} {{.ID}} {{.Status}}'`，留作部署后对比。

- [ ] **Step 4: 部署后核对工具发现**：OpenConnector `dify-db` 的 `custom_mcp.list_tools` 返回 4 个工具，两个新工具 `operationType` 为 `read`。

- [ ] **Step 5: kube_read 线上校验**
  - `prod`、`["get","deploy","dify-api-oltp","-n","dify-plugin","-o","wide"]` → 成功，含镜像。
  - `prod`、`["get","pods","-n","dify-plugin","-l","app=dify-api-oltp"]` → 成功（确认 `-l` 没被 tsh 当成 `--login`）。
  - `prod`、`["get","secrets","-n","dify-plugin"]` → `KUBECTL_ERROR`，Teleport 的 Forbidden。
  - `prod`、`["delete","pod","x"]` → `INVALID_ARGUMENT`。
  - `staging`、`["get","pods","-n","dify-staging","--no-headers"]` → 成功。

- [ ] **Step 6: plugin_lambda_logs 线上校验**（证书已在 2026-11-27 19:28 续期后带上 `support-lambda-logs`，见 spec §9；若校验时角色又没了，把 `lambda_logs.enabled` 设为 `false` 并告知用户）
  - `function_name=9a442b2d0d9b43e56a190df6ca7dc207`、`since_minutes=10`、`filter_pattern="\"REPORT RequestId\""` → 有事件。
  - 用一个真实工作空间的 `tenant_id` + `plugin_id=langgenius/openai` → 返回 `plugin_unique_identifier` 和事件或空结果提示。
  - 同一函数、最近 3 天、`filter_pattern="\"Traceback\""` → 反复用 `next_token` 直到 `complete=true`，每次调用耗时 < 60 秒。

- [ ] **Step 7: 校正登录判定**：部署后的第一次 `kube_read` 和第一次 `plugin_lambda_logs` 天然走「没有 kubeconfig / 没有 app 证书」的路径。查 `docker logs db-gateway` 里这两次调用中 tsh 的实际报错，与 `KUBE_LOGIN_MARKERS` 和 Task 4 的 `needs_login` 对照；不一致就改常量、补测试，重新走本任务。

- [ ] **Step 8: 连接池不受影响**：Step 5–7 期间 `docker logs db-gateway | grep 'teleport keys changed'` 没有随登录出现的新记录；`db_gateway_query` 跑 `SELECT 1` 正常。

- [ ] **Step 9: 续期后自愈**：等下一次 watchdog 续期（`docker logs tsh-auth-watchdog | grep auto_renew_success`）后，两个工具各调一次成功，无人工操作。

- [ ] **Step 10: 其他容器未动**：与 Step 3 对比，除 `db-gateway` 外容器 ID 和启动时间不变，`tsh-proxy-*`、`tsh-auth-watchdog` 未重启。

- [ ] **Step 11: 把线上校验结果（含 `tsh apps login` 和 `tsh aws` 的实际执行情况）补到 spec §9 并提交。**
