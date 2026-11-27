# dify-db Teleport 只读工具（plugin_lambda_logs、kube_read）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 db-gateway 里新增两个只读 MCP 工具：`plugin_lambda_logs`（查插件 Lambda 的 CloudWatch 日志）和 `kube_read`（只读 kubectl），复用网关已有的 Teleport 会话。

**Architecture:** 新增 `teleport_cli.py`：
- 影子 `TELEPORT_HOME`：只读线上 `.tsh`，在容器内副本里运行 tsh；
- tsh 子进程执行层：统一截止时间、进程组终止、输出上限、按需登录。

两个工具各一个模块，在执行层之上做参数校验和结果整理；`mcp_schema.py`/`mcp.py` 按配置把新工具加进 `tools/list` 和分发。镜像自带 tsh 18.11.0 给新工具用。不加容器、不加 Secret、不改 watchdog、隧道容器、宿主机 tsh 和 `pg_pool.py`。

**Tech Stack:** Python 3.12、aiohttp、pydantic、pytest（`asyncio_mode=auto`）、uv；运行时依赖镜像内的 tsh 18.11.0（`/opt/teleport/bin/tsh`）和 aws CLI v1（pip `awscli`）。宿主机挂载的 tsh 18.0.0 只用于现有功能和 `tsh status` 判定。

**Spec:** `docs/superpowers/specs/2026-11-27-dify-db-teleport-read-tools-design.md`（Task 0 实测见 §9.2）

## Global Constraints

- 只改 `db-gateway/` 目录（文档在仓库根 `docs/`）。不改端口、网络、挂载、Secret、`pg_pool.py`、其他服务目录；不重启或修改 `tsh-proxy-*`、`tsh-auth-watchdog`；不改宿主机 `/usr/local/bin/tsh`。
- 所有命令在 `db-gateway/` 下执行：`uv sync --locked --all-extras --all-groups`、`uv run --locked pytest -q`。
- 子进程一律用参数列表（`asyncio.create_subprocess_exec`），不经 shell，并且 `start_new_session=True`。
- 新工具调用的 tsh 一律是 `cfg.teleport_tools.tsh_path`。子进程环境：
  - 设置：`TELEPORT_HOME=<cfg.teleport_tools.shadow_home>`、`KUBECONFIG=<cfg.teleport_tools.kube.kubeconfig_path>`、`TELEPORT_TOOLS_VERSION=off`；
  - **删除**：`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及其小写形式；
  - 不设 `NO_PROXY=*`：`tsh aws` 要靠给 aws 子进程设 `HTTPS_PROXY` 走本地代理。
- **新工具从不写线上 `cfg.teleport.auth_home`**，只读它来同步影子目录、跑 `tsh status`。
- **统一截止时间**：每次工具调用开始时算 `deadline = time.monotonic() + time_budget_seconds`，之后的 SQL、登录、每次 tsh 调用都只用剩余时间，不设「至少 N 秒」的下限。单次工具调用总耗时必须小于 60 秒（Nginx `/db-gateway/` 路由 `proxy_read_timeout 60s`），`time_budget_seconds` 默认 40。
- 工具返回都是 dict，带 `success`（bool）、`code`（成功为 `""`）、`error`（成功为 `None`）、`elapsed_ms`，与现有 `execute_schema_lookup` 一致。
- 新工具 schema 的 `annotations` 固定为 `{"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}`。
- `mcp_schema.TOOLS` 保持为现有两个工具不变（stdio 脚本和现有测试依赖它）。
- 审计日志不写日志内容和 kubectl 输出。
- 不做防读本地文件的参数限制（用户确认单人使用）；kubectl 参数校验只防误操作和换身份。
- 提交信息结尾按会话要求附 Co-Authored-By 和 Claude-Session 两行。推送到 `main`（即生产部署）需要用户明确授权。

## Review Focus

1. **线上 `.tsh` 不被写**：tsh 18.11 每次操作都重写整个 key ring，只能靠影子目录隔离 → Task 2 全部用例、Task 3 `test_source_never_written`。
2. **杀进程组**：`tsh aws` 有 aws 子进程，只杀 tsh 时管道还开着 → Task 3 `test_deadline_kills_process_group`。
3. **统一截止时间**：截止时间已过不启动子进程、登录超时取剩余时间 → Task 3 `test_past_deadline_does_not_spawn`、`test_login_timeout_uses_remaining_time`；Task 5 `test_kube_login_shares_deadline`。
4. **kubectl 退出码永远是 0**：错误只看 stderr，`No resources found` 不算错误 → Task 5 `test_kube_read_forbidden_is_kubectl_error_without_login`、`test_kube_read_no_resources_is_success`。
5. **续期窗口**：线上 `keys/` 暂时为空时继续用影子里的旧证书；没有副本时返回 `TELEPORT_EXPIRED`，不能报成「没有权限」→ Task 2 `test_shadow_kept_during_renewal_window`、Task 3 `test_login_failure_without_session_is_expired`。
6. **稀疏过滤的空页**：CloudWatch 可能连续返回 `events=[]` 但带 `nextToken`，应继续翻 → Task 4 `test_empty_pages_with_token_keep_paging`。
7. **kubectl 短参数连写**：`-Aw`、`-fpods.yaml`、`--as=admin`、`--watch=true` 都要被拒；`-n`/`-o`/`-l` 后面带的值不能误判 → Task 5 `test_blocked_flags`、`test_allowed_flags`。

---

## File Structure

| 文件 | 职责 |
| --- | --- |
| `src/db_gateway/config.py`（改） | `TeleportToolsConfig` 及默认值 |
| `src/db_gateway/teleport_cli.py`（新） | `ensure_shadow_home`、`run_tsh`、`run_tsh_with_login`、`tsh_login`、`strip_ansi`、异常类型 |
| `src/db_gateway/lambda_logs.py`（新） | 时间解析、函数名定位、按页取日志、`execute_plugin_lambda_logs` |
| `src/db_gateway/kube_read.py`（新） | 参数校验、context 解析、结果判定、`execute_kube_read` |
| `src/db_gateway/mcp_schema.py`、`mcp.py`（改） | 两个 schema、`tools_for(cfg)`、分发 |
| `Dockerfile`（改） | tsh 18.11.0（固定版本、校验 sha256） |
| `pyproject.toml`、`uv.lock`、`requirements.runtime.txt`（改） | aws CLI v1 |
| `deploy/runtime.json`、`config.example.yaml`、`README.md`（改） | 配置和文档 |
| `tests/services/conftest.py`（新） | `fake_tsh`、`cfg`、`write_tsh_src`、autouse 状态重置 |
| `tests/services/test_teleport_cli.py`、`test_lambda_logs.py`、`test_kube_read.py`（新） | 单元测试 |

### 测试夹具（`tests/services/conftest.py`，Task 2 创建，后续任务复用）

**`fake_tsh`**：在 `tmp_path` 写一个可执行的 Python 脚本（shebang 用 `sys.executable`），每次被调用时：
1. 把 `argv[1:]` 和环境变量子集追加到 `calls.jsonl`。环境变量子集是 `TELEPORT_HOME`、`KUBECONFIG`、`TELEPORT_TOOLS_VERSION`，以及名字里含 `proxy` 的变量（不分大小写）。
2. 从响应队列取下一条并执行。

夹具对外提供：
- `fake_tsh.path`：脚本路径。
- `fake_tsh.push(stdout="", stdout_bytes=None, stderr="", code=0, sleep=0, child_sleep=None, write_file=None)`：往响应队列加一条。
  - `stdout_bytes`：原样写出的字节，优先于 `stdout`。
  - 先写 stdout/stderr 并 flush。
  - `child_sleep`：再起一个继承 stdout 的孙进程睡这么多秒，并把孙进程 pid 写到 `child.pid`。
  - 然后睡 `sleep` 秒，最后以 `code` 退出。
  - `write_file=[路径, 内容]`：执行时写这个文件。
  - 队列空时输出空并以 0 退出。
- `fake_tsh.until_login(code=1, stderr="no creds", on_login=None)`：
  - 在收到 `apps login` 或 `kube login` 之前，其他调用都以给定退出码和 stderr 返回；
  - 收到登录调用时建一个标记文件、按需写 `on_login=(路径, 内容)`、以 0 退出；
  - 之后的非登录调用按队列返回。
- `fake_tsh.calls`：`[{"argv": [...], "env": {...}}]`，每次读取时重新解析 `calls.jsonl`。
- `fake_tsh.child_pid()`：返回孙进程 pid。

**`write_tsh_src(root, cert="cert-1")`**：写一个最小的 TELEPORT_HOME 源目录，返回 `root`：
- `current-profile`、`teleport.dify.dev.yaml`、`bin/.config.json`；
- `keys/teleport.dify.dev/` 下：
  - `u.crt`（内容为 `cert`）、`u.key`、`u`、`certs.pem`、`cas/teleport.dify.dev.pem`、`u-ssh/teleport.dify.dev-cert.pub`；
  - `u-app/teleport.dify.dev/x.crt`、`u-kube/teleport.dify.dev/c.cred`、`u-db/teleport.dify.dev/d.crt`。

**`cfg`**：

```python
AppConfig(
    teleport={"auth_home": str(write_tsh_src(tmp_path / "tsh-src")),
              "proxy": "teleport.dify.dev:443", "user": "u"},
    db_gateway={"audit_log_dir": str(tmp_path / "audit")},
    teleport_tools={"tsh_path": fake_tsh.path, "shadow_home": str(tmp_path / "shadow"),
                    "kube": {"kubeconfig_path": str(tmp_path / "kube" / "kubeconfig")}},
)
```

**autouse `_reset_teleport_state`**：
- 对已导入的 `db_gateway.teleport_cli`、`db_gateway.lambda_logs`、`db_gateway.kube_read` 调用各自的 `_reset_state()`。这个函数清空模块级的锁、登录时间戳、影子签名缓存和信号量。
- 把 `db_gateway.teleport_cli.get_tsh_status` 替换为 `AsyncMock(return_value=None)`，防止测试跑到真的 tsh。需要「已登录」的用例自己覆盖。

**`snapshot(path)`**：返回 `{相对路径: (size, mtime_ns, sha256)}`，用于断言源目录没被写。

---

### Task 0: 服务器实测（已完成）

2026-11-27 20:45–21:00 KST 在首尔服务器完成。做法：
- 把 `.tsh` 复制到临时目录，在一次性容器（db-gateway 当前镜像，host 网络）里跑；
- 线上 `.tsh`、db-gateway 和 tsh 相关容器没动；
- 结束后删除了临时目录（含私钥副本）。

详细结果见 spec §9.2。对计划的影响：

| 发现 | 计划里的处理 |
| --- | --- |
| 宿主机 tsh 18.0.0 跑 `tsh aws` 全部签名校验失败（403），换各版本 awscli 都一样；tsh 18.11.0 正常 | Task 7 镜像自带 tsh 18.11.0；Task 1 加 `tsh_path` |
| tsh 每次 apps/aws/kube 操作都重写 `keys/<proxy>/` 下整套用户文件（SSH 私钥内容也变） | Task 2 影子 TELEPORT_HOME；删除原「`keys_signature` 跳过 `-app`/`-kube`」任务，不改 `pg_pool.py` |
| `tsh kubectl` 报错时退出码仍是 0 | Task 5 按 stderr 判定 `KUBECTL_ERROR` 和 `needs_login` |
| `tsh aws` 有 aws 子进程，只杀 tsh 时管道 3.7 秒后才关 | Task 3 进程组终止 |
| `--filter-pattern -DEBUG` 被 aws 当成选项报错 | Task 4 用 `--filter-pattern=`、`--next-token=` 写法 |
| 18.11 的 `tsh aws` 没有 app 证书时自动登录（约 4.5 秒）；kube 证书被删后自动重签 | 按需登录只作兜底 |
| stderr 带 ANSI 颜色码（`\x1b[31mERROR: \x1b[0m`） | Task 3 `strip_ansi` |
| awscli 1.46.3 依赖：colorama 0.4.6、docutils 0.19、jmespath 1.1.0、pyasn1 0.6.5、python-dateutil 2.9.0.post0、rsa 4.7.2、six 1.17.0、urllib3 2.8.0（botocore 已内置） | Task 7 锁定版本 |
| tsh 18.11.0 安装包 `teleport-v18.11.0-linux-amd64-bin.tar.gz` 220,589,585 字节，sha256 `8d3dba03ea6b6e0d72c97e190da0595e8f0d70f5573c3a26b1908cf60caf6e3f`；其中 tsh 141,970,888 字节 | Task 7 |

---

### Task 1: 配置

**Files:**
- Modify: `db-gateway/src/db_gateway/config.py`
- Modify: `db-gateway/deploy/runtime.json`、`db-gateway/config.example.yaml`
- Test: `db-gateway/tests/services/test_config.py`

**Interfaces:**
- Produces: `cfg.teleport_tools`，字段与默认值：
  - `tsh_path: str = "/opt/teleport/bin/tsh"`、`shadow_home: str = "/tmp/db-gateway/tsh-home"`
  - `time_budget_seconds: float = 40`、`max_concurrent: int = 3`
  - `lambda_logs`: `enabled: bool = True`、`app: str = "aws-support-logs"`、`aws_role: str = "SupportLambdaLogsReadOnly"`、`region: str = "us-east-1"`、`default_timezone: str = "Asia/Shanghai"`、`default_limit: int = 1000`、`max_limit: int = 10000`
  - `kube`: `enabled: bool = True`、`clusters: dict[str, str] = {"prod": "lg-eks-01", "staging": "dify-plugin-1"}`、`max_output_bytes: int = 1048576`、`kubeconfig_path: str = "/tmp/db-gateway/kubeconfig"`

- [ ] **Step 1: 写失败的测试**

```python
def test_teleport_tools_defaults():
    t = AppConfig().teleport_tools
    assert (t.tsh_path, t.shadow_home) == ("/opt/teleport/bin/tsh", "/tmp/db-gateway/tsh-home")
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

（`ROOT` 沿用该测试文件里已有的写法 `Path(__file__).resolve().parents[2]`。）

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --locked pytest tests/services/test_config.py -q`
Expected: FAIL，`AppConfig` 没有 `teleport_tools`

- [ ] **Step 3: 在 `config.py` 加 `LambdaLogsConfig`、`KubeReadConfig`、`TeleportToolsConfig`，并挂到 `AppConfig.teleport_tools`**

- [ ] **Step 4: 改配置文件**
  - `deploy/runtime.json` 的 `settings` 里加 `"teleport_tools": {"lambda_logs": {"enabled": true}, "kube": {"enabled": true}}`。
  - `config.example.yaml` 加同名段落，列出全部键和默认值。

- [ ] **Step 5: 运行，确认通过**

Run: `uv run --locked pytest tests/services/test_config.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add db-gateway/src/db_gateway/config.py db-gateway/deploy/runtime.json db-gateway/config.example.yaml db-gateway/tests/services/test_config.py
git commit -m "db-gateway: teleport_tools 配置"
```

---

### Task 2: 影子 TELEPORT_HOME

**Files:**
- Create: `db-gateway/src/db_gateway/teleport_cli.py`（本任务只放影子目录部分）
- Create: `db-gateway/tests/services/conftest.py`（见上文「测试夹具」）
- Test: `db-gateway/tests/services/test_teleport_cli.py`

**Interfaces:**
- Consumes: `cfg.teleport.auth_home`、`cfg.teleport_tools.shadow_home`；`db_gateway.postgres.TeleportAuthExpired`
- Produces:

```python
SHADOW_SIGNATURE_FILE = ".source-signature.json"
SHADOW_IGNORE_SUFFIXES = ("-app", "-kube", "-db")

def source_signature(auth_home: str) -> list[list[str]] | None
async def ensure_shadow_home(cfg) -> str          # 返回影子目录路径
def _reset_state() -> None
```

行为约定：
- `source_signature`：
  - 取 `<auth_home>/keys/*/*.crt`，只看 proxy 目录下一层的普通文件，即用户 TLS 证书；
  - 返回按路径排序的 `[[相对路径, sha256], …]`；
  - `current-profile` 不存在或没有这类文件时返回 `None`。
  - tsh 重写 key ring 时 `.crt` 内容不变（Task 0 已验证），所以签名只随续期变化。
- `ensure_shadow_home`（在模块级 `asyncio.Lock` 内）：
  1. 算源签名 `sig`，读副本里的 `SHADOW_SIGNATURE_FILE`。
  2. `sig` 等于副本签名 → 返回副本路径。
  3. `sig` 为 `None`（续期窗口或未登录）→ 副本有签名文件就返回副本路径（旧证书仍在有效期内），否则 `raise TeleportAuthExpired("Teleport 会话不可用（可能正在续期），稍后重试")`。
  4. 否则复制到临时目录 `<shadow_home>.tmp-<pid>`：
     - 用 `shutil.copytree(…, ignore=…)`，忽略名称为 `bin` 或以 `SHADOW_IGNORE_SUFFIXES` 结尾的目录；
     - 复制后再算一次源签名，与 `sig` 不同就丢弃重试一次（复制时 watchdog 可能在续期）；
     - 仍不同或复制抛 `OSError`/`shutil.Error` 时，按第 3 步处理。
  5. 写签名文件，把旧副本 rename 成 `<shadow_home>.old-<pid>`，临时目录 rename 成 `shadow_home`，删除旧副本。
- 只读源目录，从不在源目录里创建、修改或删除任何东西。

- [ ] **Step 1: 写夹具和失败的测试**

```python
async def test_shadow_copies_without_app_kube_db_bin(cfg):
    shadow = Path(await ensure_shadow_home(cfg))
    assert shadow == Path(cfg.teleport_tools.shadow_home)
    k = shadow / "keys" / "teleport.dify.dev"
    assert (k / "u.crt").read_text() == "cert-1" and (shadow / "current-profile").exists()
    assert (k / "u-ssh" / "teleport.dify.dev-cert.pub").exists() and (k / "cas").is_dir()
    assert not any((k / d).exists() for d in ("u-app", "u-kube", "u-db"))
    assert not (shadow / "bin").exists()


async def test_shadow_not_recopied_when_signature_unchanged(cfg):
    shadow = Path(await ensure_shadow_home(cfg))
    (shadow / "keys/teleport.dify.dev/u-app").mkdir()   # tsh 在副本里签发的 app 证书
    await ensure_shadow_home(cfg)
    assert (shadow / "keys/teleport.dify.dev/u-app").exists()


async def test_shadow_resyncs_after_renewal(cfg):
    shadow = Path(await ensure_shadow_home(cfg))
    (shadow / "keys/teleport.dify.dev/u-app").mkdir()
    write_tsh_src(Path(cfg.teleport.auth_home), cert="cert-2")
    await ensure_shadow_home(cfg)
    assert (shadow / "keys/teleport.dify.dev/u.crt").read_text() == "cert-2"
    assert not (shadow / "keys/teleport.dify.dev/u-app").exists()


async def test_shadow_kept_during_renewal_window(cfg):
    shadow = await ensure_shadow_home(cfg)
    shutil.rmtree(Path(cfg.teleport.auth_home) / "keys")
    (Path(cfg.teleport.auth_home) / "current-profile").unlink()
    assert await ensure_shadow_home(cfg) == shadow
    assert (Path(shadow) / "keys/teleport.dify.dev/u.crt").read_text() == "cert-1"


async def test_no_source_and_no_shadow_is_expired(cfg):
    shutil.rmtree(Path(cfg.teleport.auth_home) / "keys")
    with pytest.raises(TeleportAuthExpired):
        await ensure_shadow_home(cfg)


async def test_shadow_sync_does_not_touch_source(cfg):
    before = snapshot(cfg.teleport.auth_home)
    await ensure_shadow_home(cfg)
    write_tsh_src(Path(cfg.teleport.auth_home), cert="cert-2")
    before2 = snapshot(cfg.teleport.auth_home)
    await ensure_shadow_home(cfg)
    assert snapshot(cfg.teleport.auth_home) == before2 and before != before2
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --locked pytest tests/services/test_teleport_cli.py -q`
Expected: FAIL，`db_gateway.teleport_cli` 不存在

- [ ] **Step 3: 实现影子目录部分（按 Interfaces 和行为约定）**

- [ ] **Step 4: 运行，确认通过**

Run: `uv run --locked pytest tests/services/test_teleport_cli.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add db-gateway/src/db_gateway/teleport_cli.py db-gateway/tests/services/conftest.py db-gateway/tests/services/test_teleport_cli.py
git commit -m "db-gateway: 影子 TELEPORT_HOME，新工具不写线上 .tsh"
```

---

### Task 3: tsh 执行层

**Files:**
- Modify: `db-gateway/src/db_gateway/teleport_cli.py`
- Test: `db-gateway/tests/services/test_teleport_cli.py`

**Interfaces:**
- Consumes: Task 1 配置；Task 2 `ensure_shadow_home`；`cfg.teleport.auth_home`、`cfg.teleport.proxy`、`cfg.teleport.user`；`db_gateway.postgres.TeleportAuthExpired`；`db_gateway.teleport.get_tsh_status`
- Produces:

```python
LOGIN_TIMEOUT_SECONDS = 20.0
STDERR_KEEP_BYTES = 64 * 1024
KILL_GRACE_SECONDS = 2.0

@dataclass
class TshResult:
    exit_code: int | None      # 被截止时间杀掉或未启动时为 None
    stdout: str                # utf-8，errors="replace"，已去 ANSI
    stderr: str                # 同上
    timed_out: bool
    truncated: bool            # stdout 超过 max_output_bytes
    elapsed_ms: int

class TeleportNotAuthorized(Exception): ...

def strip_ansi(text: str) -> str

async def run_tsh(args: list[str], *, cfg, deadline: float,
                  max_output_bytes: int | None = None) -> TshResult

async def run_tsh_with_login(args: list[str], *, cfg, deadline: float,
                             login_args: list[str],
                             needs_login: Callable[[TshResult], bool],
                             max_output_bytes: int | None = None) -> TshResult

async def tsh_login(login_args: list[str], *, cfg, deadline: float,
                    not_before: float | None = None) -> None
```

`deadline` 是 `time.monotonic()` 的绝对时间。

行为约定：
- `run_tsh`：
  - `deadline` 已过 → 不启动子进程，返回 `TshResult(None, "", "", True, False, 0)`。
  - 启动前 `os.makedirs(dirname(kubeconfig_path), exist_ok=True)`。环境按 Global Constraints 设置。可执行文件 `cfg.teleport_tools.tsh_path`，`start_new_session=True`。
  - 两个任务并行读 stdout/stderr。stdout 只保留前 `max_output_bytes` 字节但继续读完（不让子进程堵在管道上）；stderr 保留前 64 KiB。
  - `asyncio.wait_for(proc.wait(), deadline - now)`。超时则 `os.killpg(proc.pid, SIGKILL)`（忽略 `ProcessLookupError`），再 `await proc.wait()`，并标 `timed_out=True`。
  - 无论是否超时，子进程结束后都对进程组补一次 `killpg`（清理孤儿进程），然后最多等 `KILL_GRACE_SECONDS` 让读任务收尾，没收完就 cancel。
  - 解码后 `strip_ansi`。
- `run_tsh_with_login`：
  - 先执行，`needs_login(result)` 为真时：调 `tsh_login(login_args, cfg=cfg, deadline=deadline, not_before=<本次调用开始的 time.monotonic()>)`，然后用同一个 `deadline` 重试一次。
  - 截止时间已过时重试会直接返回 `timed_out`。
- `tsh_login`：
  - 按 `tuple(login_args)` 取一把模块级 `asyncio.Lock`。
  - 拿到锁后，如果 `not_before` 之后已经有一次同参数的登录成功，就直接返回。
  - 登录命令为 `run_tsh(login_args, cfg=cfg, deadline=min(deadline, now + LOGIN_TIMEOUT_SECONDS))`；成功则记下完成时间。
- 登录失败（非零退出或超时）时调 `get_tsh_status(proxy=…, user=…, auth_home=cfg.teleport.auth_home)`。查的是**源目录**，用宿主机 tsh：
  - 返回 `None` → `raise TeleportAuthExpired`；
  - 有状态 → `raise TeleportNotAuthorized(f"{stderr 前 600 字符}；当前 Roles: {', '.join(status.roles)}")`。
- `_reset_state()` 同时清空登录锁和登录时间戳。

- [ ] **Step 1: 写失败的测试**

```python
def dl(seconds):
    return time.monotonic() + seconds


async def test_run_tsh_env(cfg, fake_tsh, monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "no_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:7890")
    fake_tsh.push(stdout="ok\n")
    r = await run_tsh(["status"], cfg=cfg, deadline=dl(5))
    assert (r.exit_code, r.stdout, r.timed_out, r.truncated) == (0, "ok\n", False, False)
    call = fake_tsh.calls[0]
    assert call["argv"] == ["status"]
    assert call["env"]["TELEPORT_HOME"] == cfg.teleport_tools.shadow_home
    assert call["env"]["KUBECONFIG"] == cfg.teleport_tools.kube.kubeconfig_path
    assert call["env"]["TELEPORT_TOOLS_VERSION"] == "off"
    assert not [k for k in call["env"] if "proxy" in k.lower()]
    assert Path(cfg.teleport_tools.kube.kubeconfig_path).parent.is_dir()


async def test_run_tsh_passes_shell_metacharacters_verbatim(cfg, fake_tsh):
    fake_tsh.push()
    await run_tsh(["aws", "--filter-pattern", '"ERROR"; rm -rf / $(x)'], cfg=cfg, deadline=dl(5))
    assert fake_tsh.calls[0]["argv"][2] == '"ERROR"; rm -rf / $(x)'


async def test_run_tsh_caps_stdout_and_decodes_bad_bytes(cfg, fake_tsh):
    fake_tsh.push(stdout_bytes=b"\xff" + b"a" * 5000)
    r = await run_tsh(["x"], cfg=cfg, deadline=dl(5), max_output_bytes=1000)
    assert r.truncated is True and len(r.stdout.encode()) <= 1002 and r.exit_code == 0


async def test_run_tsh_strips_ansi(cfg, fake_tsh):
    fake_tsh.push(stderr="\x1b[31mERROR: \x1b[0mexit status 255\n", code=1)
    r = await run_tsh(["x"], cfg=cfg, deadline=dl(5))
    assert (r.exit_code, r.stderr) == (1, "ERROR: exit status 255\n")


async def test_deadline_returns_partial(cfg, fake_tsh):
    fake_tsh.push(stdout="first\n", sleep=30)
    r = await run_tsh(["x"], cfg=cfg, deadline=dl(1))
    assert r.timed_out is True and r.exit_code is None and r.stdout == "first\n"
    assert r.elapsed_ms < 4000


async def test_deadline_kills_process_group(cfg, fake_tsh):
    fake_tsh.push(stdout="first\n", child_sleep=30, sleep=30)   # 像 tsh aws 那样带一个子进程
    t0 = time.monotonic()
    r = await run_tsh(["aws"], cfg=cfg, deadline=dl(1))
    assert r.timed_out and r.stdout == "first\n" and time.monotonic() - t0 < 4
    assert not _alive(fake_tsh.child_pid())


async def test_past_deadline_does_not_spawn(cfg, fake_tsh):
    r = await run_tsh(["x"], cfg=cfg, deadline=time.monotonic() - 1)
    assert r.timed_out and r.exit_code is None and fake_tsh.calls == []


async def test_source_never_written(cfg, fake_tsh):
    await ensure_shadow_home(cfg)
    before = snapshot(cfg.teleport.auth_home)
    target = f"{cfg.teleport_tools.shadow_home}/keys/teleport.dify.dev/u.crt"
    fake_tsh.push(write_file=[target, "rewritten"])          # 模拟 tsh 重写 key ring
    await run_tsh(["apps", "login", "a"], cfg=cfg, deadline=dl(5))
    assert snapshot(cfg.teleport.auth_home) == before
    assert Path(target).read_text() == "rewritten"


async def test_login_then_retry(cfg, fake_tsh):
    fake_tsh.push(stderr="no creds", code=1)    # 第一次执行
    fake_tsh.push()                              # 登录
    fake_tsh.push(stdout="done")                 # 重试
    r = await run_tsh_with_login(["aws", "x"], cfg=cfg, deadline=dl(10),
                                 login_args=["apps", "login", "a"],
                                 needs_login=lambda res: res.exit_code != 0)
    assert r.stdout == "done"
    assert [c["argv"] for c in fake_tsh.calls] == [["aws", "x"], ["apps", "login", "a"], ["aws", "x"]]


async def test_concurrent_calls_login_once(cfg, fake_tsh):
    fake_tsh.until_login(code=1)
    await asyncio.gather(*[run_tsh_with_login(["aws", str(i)], cfg=cfg, deadline=dl(10),
        login_args=["apps", "login", "a"], needs_login=lambda r: r.exit_code != 0)
        for i in range(5)])
    assert sum(c["argv"][:2] == ["apps", "login"] for c in fake_tsh.calls) == 1


async def test_login_timeout_uses_remaining_time(cfg, fake_tsh):
    fake_tsh.push(code=1); fake_tsh.push(sleep=30)          # 登录卡住
    t0 = time.monotonic()
    with pytest.raises(TeleportAuthExpired):                 # get_tsh_status 默认被 mock 成 None
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(2),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)
    assert time.monotonic() - t0 < 4


async def test_login_failure_without_session_is_expired(cfg, fake_tsh):
    fake_tsh.push(code=1)
    fake_tsh.push(stderr="ERROR: No proxy address specified, missed --proxy flag?", code=1)
    with pytest.raises(TeleportAuthExpired):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(10),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)


async def test_login_failure_with_session_is_not_authorized(cfg, fake_tsh, monkeypatch):
    status = TshStatus(user="u", cluster="c", roles=["production-read-all"],
                       valid_until=datetime.now(timezone.utc), remaining=timedelta(hours=1))
    get_status = AsyncMock(return_value=status)
    monkeypatch.setattr("db_gateway.teleport_cli.get_tsh_status", get_status)
    fake_tsh.push(code=1); fake_tsh.push(stderr='ERROR: app "a" not found', code=1)
    with pytest.raises(TeleportNotAuthorized, match="production-read-all"):
        await run_tsh_with_login(["aws"], cfg=cfg, deadline=dl(10),
                                 login_args=["apps", "login", "a"], needs_login=lambda r: True)
    assert get_status.call_args.kwargs["auth_home"] == cfg.teleport.auth_home
```

`_alive(pid)` 是测试文件里的辅助函数：读 `/proc/<pid>/stat`，文件不存在或状态为 `Z`（僵尸进程）时返回 `False`。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run --locked pytest tests/services/test_teleport_cli.py -q`
Expected: 新测试 FAIL（`run_tsh` 等不存在），Task 2 的测试 PASS

- [ ] **Step 3: 实现（按 Interfaces 的签名和行为约定）**

- [ ] **Step 4: 运行，确认通过**

Run: `uv run --locked pytest tests/services/test_teleport_cli.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add db-gateway/src/db_gateway/teleport_cli.py db-gateway/tests/services/test_teleport_cli.py
git commit -m "db-gateway: tsh 执行层（统一截止时间、进程组终止、按需登录）"
```

---

### Task 4: plugin_lambda_logs

**Files:**
- Create: `db-gateway/src/db_gateway/lambda_logs.py`
- Test: `db-gateway/tests/services/test_lambda_logs.py`

**Interfaces:**
- Consumes:
  - Task 1 配置；
  - Task 2 `ensure_shadow_home`；Task 3 `run_tsh_with_login`、`TshResult`、`TeleportNotAuthorized`；
  - `db_gateway.core` 的 `_db_target`、`_metadata_kwargs`、`_query_limiter`、`_audit_path`；
  - `db_gateway.pg_pool.run_metadata_query`；
  - `db_gateway.postgres` 的 `TeleportAuthExpired`、`audit_log`。
- Produces:

```python
def parse_time_ms(value: Any, *, default_timezone: str) -> int      # 失败抛 ValueError
async def execute_plugin_lambda_logs(*, cfg, function_name=None, tenant_id=None, plugin_id=None,
        start_time=None, end_time=None, since_minutes=None, filter_pattern=None,
        log_stream_names=None, limit=None, next_token=None) -> dict[str, Any]
def _reset_state() -> None
```

成功返回的键：
- `success`、`code`、`error`、`elapsed_ms`；
- `function_name`、`log_group`、`region`、`plugin_unique_identifier`（仅按工作空间定位时）；
- `window`：`{"start_utc", "end_utc", "start_local", "end_local", "timezone"}`，ISO 字符串，`end` 为不含端点；
- `events`：`[{"timestamp", "log_stream", "event_id", "message"}]`，`timestamp` 为 UTC ISO 毫秒精度；
- `event_count`、`next_token`、`complete`、`warnings`。

决定：
- 调用开始时 `deadline = time.monotonic() + cfg.teleport_tools.time_budget_seconds`，后面所有步骤共用。
- `parse_time_ms`：
  - `bool` → `ValueError`。
  - `int`/`float` 或纯数字字符串按毫秒时间戳；小于 `1e11` → `ValueError("看起来是秒级时间戳，请改用毫秒")`。
  - 其余用 `datetime.fromisoformat`（先把结尾的 `Z` 换成 `+00:00`）；无时区的用 `ZoneInfo(default_timezone)`。
- 定位：
  - `function_name` 与 `tenant_id`+`plugin_id` 必须恰好给一种，否则 `INVALID_ARGUMENT`。
  - `function_name` 须匹配 `^[A-Za-z0-9_-]{1,64}$`。`tenant_id` 用 `uuid.UUID()` 校验；`plugin_id` 须含恰好一个 `/`。
- 定位 SQL：`plugin` 库，绑定参数 `(tenant_id, plugin_id)`，`run_metadata_query(timeout=10)`；外层用 `asyncio.wait_for` 限制为 `min(15, 剩余时间)`，并套 `_query_limiter("plugin", port, cfg.postgres.max_concurrent_queries)`：

```sql
SELECT i.plugin_unique_identifier, i.runtime_type, r.function_name, r.function_url
FROM public.plugin_installations AS i
LEFT JOIN public.serverless_runtimes AS r
  ON r.plugin_unique_identifier = i.plugin_unique_identifier
WHERE i.tenant_id = %s::uuid AND i.plugin_id = %s
```

  查询结果的处理：
  - 无行 → `UNKNOWN_FUNCTION`「该工作空间没有安装 {plugin_id}」。
  - `function_name` 为空 → `UNKNOWN_FUNCTION`「有安装记录（runtime_type={…}）但没有运行映射」。
  - `function_url` 不匹配 `^https://[^/]+\.lambda-url\.{region}\.on\.aws(/|$)` → `UNKNOWN_FUNCTION`「运行后端不是 Lambda {region}」。
  - 数据库的 `TeleportAuthExpired` → `TELEPORT_EXPIRED`；超时和其他异常 → `QUERY_ERROR`。
  - 按工作空间定位成功时，`warnings` 加一条：「安装表反映当前状态；故障后升级或重装过，请按故障时的版本用 db_gateway_query 查 serverless_runtimes。」
- 窗口：
  - `since_minutes`（正整数，不接受 `bool`）与 `start_time`+`end_time` 恰好给一种；`end > start`。
  - 传给 CloudWatch 的是 `--start-time start_ms`、`--end-time end_ms - 1`。
- `limit`：缺省 `default_limit`；大于 `max_limit` 时取 `max_limit` 并加 warning；小于 1 → `INVALID_ARGUMENT`。
- 参数校验通过后、第一次 tsh 调用前调 `ensure_shadow_home(cfg)`。
- 每页命令（`remaining = limit - 已取条数`；用户给的值一律用 `=` 写法）：

```
aws --app <app> logs filter-log-events --region <region>
    --log-group-name /aws/lambda/<fn> --start-time <ms> --end-time <ms>
    [--filter-pattern=<p>] [--log-stream-names <a> <b> …]
    --limit <min(remaining, 10000)> --no-paginate [--next-token=<t>] --output json
```

  - aws CLI v1 没有 `--no-cli-pager`，不要加。
  - `login_args = ["apps", "login", "--aws-role", <aws_role>, <app>]`。
  - `needs_login = lambda r: r.exit_code not in (0, None) and "An error occurred (" not in r.stderr and "aws: error:" not in r.stderr`：AWS 接口错误和 aws 参数错误都不登录。tsh 18.11 平时会自动登录 app，这只是兜底。
- 翻页循环：每页都用同一个 `deadline`。一页成功后把 `events` 追加、记下 `nextToken`。停止条件按顺序判断：
  1. 没有 `nextToken` → `complete=True, next_token=None`；
  2. 已取条数 ≥ `limit` → 返回 `nextToken`，warning「已达到 limit」；
  3. 已用时间 ≥ `time_budget_seconds * 0.6` → 返回 `nextToken`，warning「时间预算用完，用 next_token 继续」。
- 某页失败：
  - `timed_out` 且是第一页 → `success=False`、`code="LOGS_TIMEOUT"`，`error` 提示缩小窗口或加 `filter_pattern`。
  - 非零退出且是第一页 → `success=False`、`code="AWS_ERROR"`，`error` = stderr 前 800 字符。
  - 之前已有成功的页（不论超时还是非零退出）→ `success=True`，返回已取到的事件和上一页的 `nextToken`，`complete=False`，加一条 warning 说明中断原因。
  - `TeleportAuthExpired` → `TELEPORT_EXPIRED`；`TeleportNotAuthorized` → `NOT_AUTHORIZED`，`error` 后附「需要 Teleport 角色 support-lambda-logs」。
- `complete=True` 且 `event_count == 0` 时加 warning：「没有匹配的日志：先去掉 filter_pattern、把窗口放宽到故障前后各 5 分钟，再核对时区和该工作空间实际安装的 checksum；同一函数被多个工作空间共用，关联客户请求需要时间或 RequestId。」
- 并发：模块级 `asyncio.Semaphore(cfg.teleport_tools.max_concurrent)`，按 `max_concurrent` 值惰性创建；`_reset_state()` 清空。
- 审计：

```python
audit_log(log_path=_audit_path(cfg), database="", sql="", status=<"OK" 或 code>,
          row_count=event_count, elapsed_ms=…, error=…, tool="plugin_lambda_logs",
          function_name=…, log_group=…, start_ms=…, end_ms=…, filter_pattern=…, limit=…,
          has_next_token=bool(next_token 入参), complete=…)
```

- [ ] **Step 1: 写失败的测试**

辅助：
- `page(events, token=None)` 返回 `json.dumps({"events": [...], **({"nextToken": token} if token else {})})`；
- `EV = {"timestamp": 1795775460000, "logStreamName": "s", "eventId": "1", "message": "m", "ingestionTime": 1795775460100}`（实测事件还带 `ingestionTime`，顶层还有 `searchedLogStreams`，都忽略）。

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


@pytest.mark.parametrize("value", [1795775460, "1795775460", True, "昨天"])
def test_parse_time_ms_rejects(value):
    with pytest.raises(ValueError):
        parse_time_ms(value, default_timezone="Asia/Shanghai")


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
    assert '--filter-pattern="ERROR"' in argv
    assert argv[argv.index("--limit") + 1] == "1000"
    assert "--no-paginate" in argv and "--no-cli-pager" not in argv


async def test_dash_filter_and_token_use_equals_form(cfg, fake_tsh):
    fake_tsh.push(stdout=page([EV]))
    await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5,
                                     filter_pattern="-DEBUG", next_token="T0")
    argv = fake_tsh.calls[0]["argv"]
    assert "--filter-pattern=-DEBUG" in argv and "--next-token=T0" in argv


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
    assert "--next-token=T1" in fake_tsh.calls[1]["argv"]


async def test_time_budget_returns_partial_with_token(cfg, fake_tsh):
    cfg.teleport_tools.time_budget_seconds = 2
    fake_tsh.push(stdout=page([EV], token="T1"), sleep=1.5); fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"] and (out["event_count"], out["next_token"], out["complete"]) == (1, "T1", False)


async def test_first_page_timeout_is_logs_timeout(cfg, fake_tsh):
    cfg.teleport_tools.time_budget_seconds = 1
    fake_tsh.push(sleep=30)
    t0 = time.monotonic()
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert (out["success"], out["code"]) == (False, "LOGS_TIMEOUT") and time.monotonic() - t0 < 4


async def test_aws_error_is_reported_without_login(cfg, fake_tsh):
    fake_tsh.push(code=1, stderr="An error occurred (ResourceNotFoundException) when calling "
                  "the FilterLogEvents operation: The specified log group does not exist.\n"
                  "ERROR: exit status 255\n")
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["code"] == "AWS_ERROR" and "ResourceNotFoundException" in out["error"]
    assert len(fake_tsh.calls) == 1


async def test_aws_usage_error_does_not_login(cfg, fake_tsh):
    fake_tsh.push(code=1, stderr="aws: error: argument --filter-pattern: expected one argument\n")
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["code"] == "AWS_ERROR" and len(fake_tsh.calls) == 1


async def test_missing_app_cert_logs_in_and_retries(cfg, fake_tsh):
    fake_tsh.push(code=1, stderr="ERROR: some tsh error"); fake_tsh.push(); fake_tsh.push(stdout=page([EV]))
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f" * 32, since_minutes=10)
    assert out["success"]
    assert fake_tsh.calls[1]["argv"] == ["apps", "login", "--aws-role", "SupportLambdaLogsReadOnly",
                                         "aws-support-logs"]


async def test_no_teleport_session_is_expired_without_running_tsh(cfg, fake_tsh):
    shutil.rmtree(Path(cfg.teleport.auth_home) / "keys")
    out = await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    assert out["code"] == "TELEPORT_EXPIRED" and fake_tsh.calls == []


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
    {"function_name": "f", "start_time": 1795775520000, "end_time": 1795775460000},
    {"function_name": "f", "start_time": 1795775460, "end_time": 1795775520},   # 秒级
    {"function_name": "f", "since_minutes": 0},
    {"function_name": "f", "since_minutes": True},
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
    fake_tsh.push(stdout=page([{**EV, "message": "SECRET-PAYLOAD"}]))
    await execute_plugin_lambda_logs(cfg=cfg, function_name="f", since_minutes=5)
    text = (Path(cfg.db_gateway.audit_log_dir) / "db_gateway_audit.log").read_text()
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
- Consumes:
  - Task 1 配置；
  - Task 2 `ensure_shadow_home`；Task 3 `run_tsh_with_login`、`tsh_login`、`TshResult`、`TeleportNotAuthorized`；
  - `db_gateway.core._audit_path`；
  - `db_gateway.postgres` 的 `TeleportAuthExpired`、`audit_log`。
- Produces:

```python
KUBE_LOGIN_MARKERS: tuple[re.Pattern, ...]
KUBECTL_ERROR_RE: re.Pattern
def validate_kubectl_args(args: Any) -> list[str]            # 不合法抛 ValueError（中文原因）
def resolve_context(kubeconfig_path: str, *, proxy: str, cluster_name: str) -> str | None
def kubectl_failed(result: TshResult) -> bool
async def execute_kube_read(*, cfg, cluster: Any, args: Any) -> dict[str, Any]
def _reset_state() -> None
```

成功返回的键：`success`、`code`、`error`、`elapsed_ms`、`cluster`、`context`、`output`、`stderr`、`exit_code`、`truncated`、`timed_out`、`warnings`。

决定：
- `validate_kubectl_args`（与原计划相同，只防误操作和换身份，不限制 `-o` 格式）：
  - 必须是非空的字符串列表，最多 64 项，单项最长 4096 字符，不含 `\x00`。
  - `args[0]` 属于 `{"get", "describe", "logs", "top", "explain", "api-resources", "api-versions", "version", "auth"}`；`auth` 时 `args[1]` 必须是 `can-i`。
  - 只检查第一个 `--` 之前的参数。
  - 长参数：参数等于下列之一，或以「下列之一 + `=`」开头，就拒绝：`--filename --kustomize --kubeconfig --context --cluster --user --as --as-group --as-uid --token --server --certificate-authority --client-certificate --client-key --insecure-skip-tls-verify --tls-server-name --username --password --follow --watch --watch-only --cache-dir --profile --profile-output`。
  - 短参数（以单个 `-` 开头、长度 ≥ 2、不以 `--` 开头）：从第二个字符起逐个看；遇到 `n o l c L` 之一就停（后面是它的值）；在停之前遇到 `f k w s` 之一就拒绝。
  - 错误信息说明是哪一个参数、属于哪一类（读本地文件 / 换身份或目标 / 持续输出 / 写本地文件）。
- `resolve_context`：
  - 用 `yaml.safe_load` 读 kubeconfig，`host = proxy.split(":")[0]`；
  - 在 `contexts[].name` 里找等于 `f"{host}-{cluster_name}"` 或以 `f"{host}-{cluster_name}-"` 开头的，恰好一个则返回它；
  - 文件不存在、解析失败、0 个或多个时返回 `None`。
  - 实测 context 名为 `teleport.dify.dev-lg-eks-01-us-east-1-523286616386`、`teleport.dify.dev-dify-plugin-1-eks-us-east-1-014498624057`。
- `KUBECTL_ERROR_RE = re.compile(r"^(error:|Error from server|Error in configuration|Unable to connect to the server)", re.M)`。`kubectl_failed(r)` = `r.exit_code not in (0, None) or KUBECTL_ERROR_RE.search(r.stderr)`。`tsh kubectl` 的退出码永远是 0（Task 0 实测）；`No resources found in … namespace.` 不匹配，不算错误。
- `KUBE_LOGIN_MARKERS`：`getting credentials`、`tsh kube login`、`exec plugin`、`context was not found`、`context "[^"]+" does not exist` 的正则。`needs_login = lambda r: any(p.search(r.stderr) for p in KUBE_LOGIN_MARKERS)`，不看退出码。这是模块常量，Task 8 遇到真实凭据错误时再校正。
- `execute_kube_read`：
  - 调用开始时算 `deadline`。
  - `cluster` 不在 `cfg.teleport_tools.kube.clusters` → `INVALID_ARGUMENT`，`error` 列出可用别名。参数校验失败 → `INVALID_ARGUMENT`。
  - 校验通过后调 `ensure_shadow_home(cfg)`。
  - `login_args = ["kube", "login", "--all"]`。先 `resolve_context`：
    - 为 `None` 时调 `tsh_login(login_args, cfg=cfg, deadline=deadline, not_before=<调用开始时间>)`，再 `resolve_context`；
    - 仍为 `None` → `NOT_AUTHORIZED`「Teleport 里看不到集群 {cluster_name}」。
  - 命令：`["kubectl", args[0], "--context", <context>, *args[1:]]`（`--context` 紧跟子命令，不会落到用户参数的 `--` 后面）。
  - `run_tsh_with_login(…, deadline=deadline, max_output_bytes=cfg.teleport_tools.kube.max_output_bytes)`。
  - 结果：
    - `kubectl_failed` → `success=False`、`code="KUBECTL_ERROR"`、`error`=stderr 前 2000 字符，`output` 照常返回；
    - 否则 `timed_out` → `success=True`，warning「到时间预算被中止，输出不完整；用 --tail、--since、-l 缩小范围」；
    - 其余 → 成功。
    - `truncated` 时加 warning「输出超过 {max_output_bytes} 字节，只保留开头；用 --tail、--since、--since-time、-l 缩小范围」。
  - `TeleportAuthExpired` → `TELEPORT_EXPIRED`；`TeleportNotAuthorized` → `NOT_AUTHORIZED`。
  - 并发：模块级信号量，同 Task 4。
  - 审计：`tool="kube_read"`、`cluster`、`args`（完整列表）、`exit_code`、`output_bytes`、`truncated`、`timed_out`、`kubectl_error`（bool）；`row_count=0`；不写输出。

- [ ] **Step 1: 写失败的测试**

辅助：
- `write_kubeconfig(path, names)`：写一个含这些 context 名的最小 kubeconfig；
- `PROD = "teleport.dify.dev-lg-eks-01-us-east-1-523286616386"`；
- `KUBECONFIG_TEXT` = 含 PROD 和 staging 两个 context 的 kubeconfig 文本；
- `kubeconfig` 夹具：把 `KUBECONFIG_TEXT` 写到 `cfg.teleport_tools.kube.kubeconfig_path`。

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
    assert fake_tsh.calls[0]["env"]["TELEPORT_HOME"] == cfg.teleport_tools.shadow_home


async def test_kube_read_logs_in_when_kubeconfig_missing(cfg, fake_tsh):
    fake_tsh.until_login(code=0, on_login=(cfg.teleport_tools.kube.kubeconfig_path, KUBECONFIG_TEXT))
    fake_tsh.push(stdout="ok")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
    assert out["success"]
    assert ["kube", "login", "--all"] in [c["argv"] for c in fake_tsh.calls]


async def test_kube_first_login_once_under_concurrency(cfg, fake_tsh):
    fake_tsh.until_login(code=0, on_login=(cfg.teleport_tools.kube.kubeconfig_path, KUBECONFIG_TEXT))
    await asyncio.gather(*[execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
                           for _ in range(3)])
    assert sum(c["argv"][:2] == ["kube", "login"] for c in fake_tsh.calls) == 1


async def test_kube_login_shares_deadline(cfg, fake_tsh):
    cfg.teleport_tools.time_budget_seconds = 2
    fake_tsh.push(sleep=30)                                   # kube login 卡住
    t0 = time.monotonic()
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
    assert out["code"] in ("TELEPORT_EXPIRED", "NOT_AUTHORIZED") and time.monotonic() - t0 < 4


async def test_kube_read_relogs_in_on_credential_error(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(code=0, stderr="Unable to connect to the server: getting credentials: exec: ...")
    fake_tsh.push(); fake_tsh.push(stdout="ok")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods"])
    assert out["success"] and out["output"] == "ok" and len(fake_tsh.calls) == 3


async def test_kube_read_forbidden_is_kubectl_error_without_login(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(code=0, stderr='Error from server (Forbidden): secrets is forbidden: User "u" cannot list')
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "secrets", "-n", "dify-plugin"])
    assert (out["success"], out["code"], out["exit_code"]) == (False, "KUBECTL_ERROR", 0)
    assert "Forbidden" in out["error"] and len(fake_tsh.calls) == 1


@pytest.mark.parametrize("stderr", [
    "error: unknown flag: --no-such-flag\nSee 'kubectl get --help' for usage.\n",
    'Error from server (NotFound): deployments.apps "x" not found\n',
])
async def test_kube_read_other_errors_with_exit_zero(cfg, fake_tsh, kubeconfig, stderr):
    fake_tsh.push(code=0, stderr=stderr)
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "deploy", "x"])
    assert out["code"] == "KUBECTL_ERROR"


async def test_kube_read_no_resources_is_success(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(code=0, stderr="No resources found in dify-plugin namespace.\n")
    out = await execute_kube_read(cfg=cfg, cluster="prod", args=["get", "pods", "-n", "dify-plugin"])
    assert out["success"] and out["stderr"].startswith("No resources found")


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


async def test_kube_read_audit_has_args_but_no_output(cfg, fake_tsh, kubeconfig):
    fake_tsh.push(stdout="CUSTOMER-DATA")
    await execute_kube_read(cfg=cfg, cluster="prod", args=["logs", "p", "-n", "dify-plugin"])
    text = (Path(cfg.db_gateway.audit_log_dir) / "db_gateway_audit.log").read_text()
    assert '"tool": "kube_read"' in text and '"dify-plugin"' in text and "CUSTOMER-DATA" not in text
```

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
- `tools_for(cfg)`：
  - 返回 `TOOLS` 的两项；`getattr(cfg.teleport_tools.lambda_logs, "enabled", False) is True` 时加上日志工具，kube 同理。用 `is True`，这样现有测试里的 `MagicMock` 配置不会意外开启。
  - kube 的 schema 深拷贝后，把 `inputSchema.properties.cluster.enum` 设为 `list(cfg.teleport_tools.kube.clusters)`，并在 `cluster` 的 description 里写出「别名=Teleport 集群名」。
- `mcp.py`：
  - `tools/list` 用 `tools_for(cfg)`；
  - `tools/call` 对未启用的工具返回 `-32601 unknown tool`；
  - 启用时把 `arguments` 里的已知键原样传给对应函数，未知键忽略。
- `plugin_lambda_logs` 的 `inputSchema`：`function_name`、`tenant_id`、`plugin_id`、`start_time`（`["string","number"]`）、`end_time`、`since_minutes`（integer，minimum 1）、`filter_pattern`、`log_stream_names`（string 数组）、`limit`（integer，minimum 1）、`next_token`；`required` 为空。
- `plugin_lambda_logs` 的 description 要点（英文，与现有工具一致）：
  - 查 SaaS 插件 Lambda 的 CloudWatch 日志（us-east-1，`/aws/lambda/<function_name>`）；定位二选一。
  - 时间不带时区按 Asia/Shanghai，数字时间戳必须是毫秒。
  - `complete=false` 时用 `next_token` 和相同的其他参数继续。
  - 事件多、消息长会占很多上下文，按需调小 `limit` 或加 `filter_pattern`。
  - `REPORT` 行只是调用统计；查报错用 `"ERROR"` 或 `"Traceback"`，已知 RequestId 时用 `"<id>"`。
  - 同一函数被多个工作空间共用。
  - 要看某插件全部版本和 checksum，用 `db_gateway_query`（`database=plugin`）跑：

```sql
SELECT r.plugin_unique_identifier, r.function_name, r.updated_at
FROM public.serverless_runtimes AS r
WHERE split_part(r.plugin_unique_identifier, '@', 1) = 'langgenius/openai:1.2.1'
ORDER BY r.updated_at DESC
```

- `kube_read` 的 `inputSchema`：`cluster`（enum）、`args`（string 数组，minItems 1）；两者 required。
- `kube_read` 的 description 要点：
  - 只读 kubectl，列出允许的子命令。
  - `--context` 由网关加，不要传；不支持 `-f/--follow`、`-w/--watch`。
  - 当前 Teleport 角色能读 `pods`、`pods/log`、`deployments`，其他资源会被 Teleport 拒绝（返回 `KUBECTL_ERROR` 和原始错误）。
  - 生产 SaaS 在 `prod` 集群的 `dify-plugin` 命名空间，staging 在 `staging` 集群的 `dify-staging`；部署的标签是 `workload.user.cattle.io/workloadselector=apps.deployment-<命名空间>-<部署名>`。
  - 输出上限 1 MB，日志用 `--tail`、`--since`、`--since-time` 控制范围。
  - 给 4 个示例（spec §5.4 的那 4 条）。
- README：新增「Teleport 只读工具」一节，内容包括：
  - 两个工具的参数表、返回字段、错误码（`UNKNOWN_FUNCTION`、`NOT_AUTHORIZED`、`AWS_ERROR`、`LOGS_TIMEOUT`、`KUBECTL_ERROR`）；
  - 配置表；
  - 镜像自带 tsh 18.11 的原因、影子 TELEPORT_HOME 的同步规则；
  - 按需登录和续期后的行为；
  - `kube_read` 的拒绝规则和按 stderr 判错；
  - 「新工具只走 /mcp，没有 HTTP 端点和 stdio 转发」。

- [ ] **Step 1: 写失败的测试**

夹具：
- `mcp_client_factory`：返回一个 async context manager 工厂 `factory(teleport_tools)`。它用与现有 `mcp_client` 相同的 `MagicMock` 配置，把 `cfg.teleport_tools` 设为传入的真实对象，起 `TestClient`。
- `teleport_mcp_client`：`factory(AppConfig().teleport_tools)`。
- 辅助：`LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}`；`call(name, args)` 生成 `tools/call` 消息。

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

### Task 7: 镜像加入 aws CLI 和 tsh 18.11

**Files:**
- Modify: `db-gateway/Dockerfile`
- Modify: `db-gateway/pyproject.toml`、`db-gateway/uv.lock`、`db-gateway/requirements.runtime.txt`
- Test: `db-gateway/tests/services/test_config.py`（Dockerfile 一致性）

- [ ] **Step 1: 写失败的测试**

```python
def test_dockerfile_bundles_pinned_tsh():
    text = (ROOT / "Dockerfile").read_text()
    assert "TELEPORT_VERSION=18.11.0" in text
    assert "8d3dba03ea6b6e0d72c97e190da0595e8f0d70f5573c3a26b1908cf60caf6e3f" in text
    assert AppConfig().teleport_tools.tsh_path in text      # /opt/teleport/bin/tsh
```

- [ ] **Step 2: 加 aws CLI 依赖并锁定**

Run: `uv add awscli`

然后：
- 把解析出的 `awscli` 及其新增传递依赖的精确版本写进 `pyproject.toml` 的 `[tool.uv] constraint-dependencies` 和 `requirements.runtime.txt`，保持字母序。Task 0 实测 awscli 1.46.3 的依赖是 colorama、docutils、jmespath、pyasn1、python-dateutil、rsa、six、urllib3（botocore 已内置），以 `uv.lock` 的 diff 为准。
- 现有固定版本（如 `pyyaml==6.0.3`）不许变；解析器要改它们时停下来报告。

- [ ] **Step 3: Dockerfile 加 tsh 18.11.0**

新增一个构建阶段：
- 用 Python 标准库下载 `https://cdn.teleport.dev/teleport-v${TELEPORT_VERSION}-linux-amd64-bin.tar.gz`，按块计算 sha256；
- 与 `ARG TELEPORT_SHA256=8d3dba03ea6b6e0d72c97e190da0595e8f0d70f5573c3a26b1908cf60caf6e3f` 不符就失败；
- 只解出 `teleport/tsh`，`chmod 0755`，并在该阶段运行一次 `tsh version` 验证。

最终镜像 `COPY --from=<阶段> … /opt/teleport/bin/tsh`。宿主机挂载的 `/usr/local/bin/tsh` 路径不变，不要把新 tsh 放到那里，否则会被挂载覆盖。

镜像预计增大约 140 MB（tsh）加 aws CLI 依赖。

- [ ] **Step 4: 确认锁定安装和测试**

Run: `uv sync --locked --all-extras --all-groups && uv run --locked pytest -q && uv run --locked aws --version`
Expected: 测试 PASS；输出 `aws-cli/1.x …`

- [ ] **Step 5: 构建镜像并确认两个 CLI**

Run: `docker build -t db-gateway:local . && docker run --rm db-gateway:local sh -c 'aws --version && /opt/teleport/bin/tsh version'`
Expected: `aws-cli/1.x …` 和 `Teleport v18.11.0 …`

- [ ] **Step 6: 镜像冒烟**

Run（仓库根）：`python3 deploy/smoke_image.py db-gateway`（参数形式以脚本为准）
Expected: health、tools/list、错误密钥拒绝通过；tools/list 数量为 4

- [ ] **Step 7: 提交**

```bash
git add db-gateway/Dockerfile db-gateway/pyproject.toml db-gateway/uv.lock db-gateway/requirements.runtime.txt db-gateway/tests/services/test_config.py
git commit -m "db-gateway: 镜像加入 aws CLI 和 tsh 18.11"
```

---

### Task 8: 上线与线上校验（需要用户授权）

推送到 `main` 要用户明确同意。合并后 CI 只应选中 `db-gateway`。

- [ ] **Step 1: 推送前核对**：`git diff --name-only main...` 只含 `db-gateway/` 和 `docs/`。

- [ ] **Step 2: PR 的 CI 通过后，核对 `plan` 作业只选中 db-gateway**；否则停下来定位。

- [ ] **Step 3: 部署前记录**（首尔服务器）：
  - `docker ps --format '{{.Names}} {{.ID}} {{.Status}}'`；
  - `find /opt/dify-db/.tsh/keys -printf '%P %s %T@\n' | sort`；
  
  两项都留作部署后对比。

- [ ] **Step 4: 部署后核对工具发现**：OpenConnector `dify-db` 的 `custom_mcp.list_tools` 返回 4 个工具，两个新工具 `operationType` 为 `read`；容器内 `/opt/teleport/bin/tsh version` 为 18.11.0。

- [ ] **Step 5: kube_read 线上校验**
  - `prod`、`["get","deploy","dify-api-oltp","-n","dify-plugin","-o","wide"]` → 成功，含镜像。
  - `prod`、`["get","pods","-n","dify-plugin","-l","workload.user.cattle.io/workloadselector=apps.deployment-dify-plugin-dify-api-oltp"]` → 成功且有 Pod。
  - `prod`、`["auth","can-i","get","pods","-n","dify-plugin"]` → `yes`。
  - `prod`、`["get","secrets","-n","dify-plugin"]` → `KUBECTL_ERROR`，Teleport 的 Forbidden，`exit_code` 为 0。
  - `prod`、`["delete","pod","x"]` → `INVALID_ARGUMENT`。
  - `staging`、`["get","pods","-n","dify-staging","--no-headers"]` → 成功。

- [ ] **Step 6: plugin_lambda_logs 线上校验**（若校验时角色没了，把 `lambda_logs.enabled` 设为 `false` 并告知用户）
  - `function_name=9a442b2d0d9b43e56a190df6ca7dc207`、`since_minutes=60`、`filter_pattern="\"REPORT RequestId\""` → 有事件。
  - 用一个真实工作空间的 `tenant_id` + `plugin_id=langgenius/openai` → 返回 `plugin_unique_identifier` 和事件或空结果提示。
  - 同一函数、最近 3 天、`filter_pattern="\"Traceback\""` → 反复用 `next_token` 直到 `complete=true`，每次调用耗时 < 60 秒。

- [ ] **Step 7: 首次调用的登录路径**：部署后第一次 `kube_read` 走「没有 kubeconfig → `tsh kube login --all`」，第一次 `plugin_lambda_logs` 走 tsh 的自动 app 登录。查 `docker logs db-gateway` 确认没有意外的登录失败；遇到 `KUBE_LOGIN_MARKERS` 没覆盖的凭据错误时改常量、补测试，重新走本任务。

- [ ] **Step 8: 线上 `.tsh` 和连接池不受影响**：
  - 与 Step 3 对比，`/opt/dify-db/.tsh/keys` 的文件列表和 mtime 只随 watchdog 续期变化，没有 `-app`、`-kube` 目录；
  - Step 5–7 期间 `docker logs db-gateway | grep 'teleport keys changed'` 没有新记录；
  - `db_gateway_query` 跑 `SELECT 1` 正常。

- [ ] **Step 9: 续期后自愈**：等下一次 watchdog 续期（`docker logs tsh-auth-watchdog | grep auto_renew_success`）后，两个工具各调一次成功，无人工操作。容器内影子目录的 `.source-signature.json` 已更新。

- [ ] **Step 10: 其他容器未动**：与 Step 3 对比，除 `db-gateway` 外容器 ID 和启动时间不变，`tsh-proxy-*`、`tsh-auth-watchdog` 未重启。

- [ ] **Step 11: 把线上校验结果补到 spec §9 并提交。**
