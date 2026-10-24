# zendesk-browser-relay

从腾讯云当前 `zendesk-browser-relay` 运行容器整理的独立 MCP 项目。端口：`8097`。全部业务代码位于本目录 `src/zendesk_browser_relay/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest
cp .env.example .env
# 填写本地凭证后启动
uv run --locked zendesk-browser-relay
```

使用 Python 3.12。本地首次使用浏览器前运行 `uv run python -m playwright install chromium`；浏览器登录资料与状态目录由配置指定，不属于源码。

## 构建

```bash
docker build -t zendesk-browser-relay:local .
```

Dockerfile 使用本目录完整源码和容器实际依赖版本，不依赖服务器上的旧镜像标签。生产配置、API 密钥、浏览器资料和业务数据应单独挂载，不能放入构建上下文。

## 发送结果

`/send` 与 MCP 发送工具返回的 `status`：

| 状态 | 含义 | 调用方处理 |
| --- | --- | --- |
| `sent` | 点击发送后，写入 Zendesk 的请求已返回 2xx，且输入框已清空 | 成功 |
| `send_failed` | 尚未点击发送就失败；或写入请求被拒绝且正文仍留在输入框 | 可以重试 |
| `send_not_confirmed` | 点击后没有观察到写入请求，正文仍留在输入框 | 先人工核对，不要自动重试 |
| `send_unknown` | 已点击发送，但结果无法确认（超时、写入未返回、页面异常等） | 消息可能已经发出，**禁止自动重试**，人工核对 |
| `login_required` / `stale_context` / `freshness_check_failed` | 未点击发送 | 按原有流程处理 |

每次发送尝试单独计时（`ZENDESK_BROWSER_OPERATION_TIMEOUT_SECONDS`，默认 300 秒）。只有点击前、页面阶段为 `channel`、`composer`、`fill`、`freshness`、`send_button` 的失败会换新浏览器重试一次。

点击后页面保持打开，直到点击触发的 Zendesk 写请求（POST/PUT/PATCH）全部返回，最长 `ZENDESK_SEND_CONFIRM_TIMEOUT_MS`（默认 15 秒）。默认把同域的所有写请求都算作发送请求；日志 `browser_relay_send_page_timing` 会记录观察到的请求方法、路径和状态码。确认线上实际的回复接口后，可用 `ZENDESK_SEND_CONFIRM_URL_PATTERN` 收窄范围，`{ticket_id}` 会被替换为工单号，例如 `/api/v2/tickets/{ticket_id}\.json`。

## 浏览器生命周期

- 登录状态通过 `/api/v2/users/me.json` 判断（共享浏览器 cookie，不加载工作台）。只有 `agent`/`admin` 视为已登录；请求失败、非 JSON 或非 200/401/403 一律返回 `unknown`，不会误报已登录。
- Zendesk 的会话 cookie 在 Chrome 退出后不会保留。登录确认后会把会话 cookie 保存到 profile 目录下的 `relay-session-cookies.json`（权限 0600，最多每 10 分钟写一次），重启后只补回 profile 中缺失的 cookie。
- 关闭浏览器超时或失败时，按 `--user-data-dir` 找到 Chrome 进程及其子进程并 `SIGKILL`，再清理 `Singleton*` 锁文件。Chrome 自行退出（崩溃）时 context 会被丢弃，下次操作重新启动；Playwright 驱动无法启动浏览器时会重启驱动再试一次。
- 不再用 `context.route("**/*")` 拦截请求（会让浏览器缓存失效），改用 `--blink-settings=imagesEnabled=false` 关闭图片。`/dev/shm` 由部署契约的 `shm_size: 256m` 提供，因此通过 `ignore_default_args` 去掉 Playwright 默认附带的 `--disable-dev-shm-usage`，让 Chrome 实际使用共享内存。
- 资源监控每 `ZENDESK_BROWSER_MEMORY_CHECK_INTERVAL_SECONDS` 秒采样一次：Chrome 进程 PSS 之和超过 `ZENDESK_BROWSER_MEMORY_LIMIT_MB`（默认 1400），或空闲时 CPU 连续 `ZENDESK_BROWSER_CPU_HIGH_SAMPLES` 次（默认 5）超过 `ZENDESK_BROWSER_CPU_LIMIT_PERCENT`（默认 80%）时回收浏览器。有操作进行时的采样不计入 CPU 判定。
- 关停：uvicorn 最多等待进行中的请求 15 秒，之后浏览器关闭最多等锁 `ZENDESK_SHUTDOWN_LOCK_TIMEOUT_SECONDS`（默认 15 秒）、关闭 context 最多 10 秒，然后强杀，整体控制在部署器 `docker stop --time 60` 之内。被中断的发送不会再重新拉起浏览器。
- `ZENDESK_MCP_URL` 已配置但令牌文件缺失、空白或不可读时，服务照常启动，`/v1/codex-completions` 等接口不受影响；只有发送会以 `freshness_check_failed` / `authorization_unavailable` 拒绝。

容器契约（`deploy/targets.json`）：`init: true`、`shm_size: 256m`、`memory: 2g`（不使用 swap）、`cpus: 1.5`；镜像内置访问 `/health` 的 Docker healthcheck，只反映 HTTP 进程存活，浏览器状态仍以鉴权 `/ready` 为准。

## 上线步骤

本次修改了 `deploy/server.py`。合并到 `main` 后，先在服务器运行 `python3 deploy/install_server.py --update-helper-only` 更新受限执行器并核对哈希；执行器未更新时 CI 部署会因哈希不一致被拒绝（安全失败，线上不受影响）。若合并触发的部署已被拒，更新执行器后手动运行工作流，`service` 填 `zendesk-browser-relay`。同时确认 GitHub Secret `MCP_ZENDESK_BROWSER_RELAY__ZENDESK_MCP_AUTHORIZATION` 已配置，它会以文件形式挂载到 `/run/mcp-secrets/zendesk_mcp_authorization`。

## 版本来源

- 原镜像：`zendesk-browser-relay:multilingual-footer-20261003`。
- 镜像 ID：`sha256:069bd86ab5f622691eb8ff36f2a911bcb6f73830056ffb9c8bbdf2a1ced9e991`。
- `baseline/runtime.json` 记录实际依赖与容器来源；`baseline/source-manifest.json` 记录原始文件哈希。
- `requirements.runtime.txt` 固定运行依赖；`pyproject.toml` 通过 constraints 保持同样版本。原锁文件和部署文本保存在 `baseline/`，不作为当前部署入口。
- 原功能说明见 [baseline/README.original.md](baseline/README.original.md)。

项目声明补齐线上代码实际导入的 `httpx` 运行依赖；版本保持与容器一致。
