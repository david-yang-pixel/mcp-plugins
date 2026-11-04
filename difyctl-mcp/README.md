# difyctl MCP

把 [difyctl](https://github.com/langgenius/dify/releases) 封装成 Streamable HTTP MCP。用户在 MCP OAuth 授权时登录自己的 Dify 账号，之后所有调用都以该账号身份执行。公开地址：`https://zendesk.smlershou.top/difyctl-mcp/mcp`。

## 架构

```text
MCP 客户端 → Nginx (HTTPS) → difyctl-mcp 容器 (0.0.0.0:8000, bridge) → difyctl 子进程 → cloud.dify.ai
```

- Python 3.12 + `mcp==2.3.0`（`MCPServer`），`stateless_http=True`、`json_response=True`，路径 `/mcp`。
- 镜像在构建时下载 `difyctl-v1.18.0-linux-x64`，按 Dockerfile 中固定的 SHA-256（来自 release 的 `difyctl-v1.18.0-checksums.txt`）校验，并执行 `difyctl install skills /opt/difyctl/skills`。容器用户为 10001，非 root。
- 部署沿用仓库部署器：bridge 网络、自动分配主机端口、只绑定 `127.0.0.1`。容器主机名固定为 `difyctl-mcp`（difyctl 用主机名作 `device_label`）。数据目录为 `@data/state` → `/var/lib/mcp/difyctl`：

| 路径 | 内容 |
| --- | --- |
| `auth.db` | OAuth 客户端、待授权记录、账号、授权码和令牌（只存 SHA-256） |
| `sessions/<随机ID>/` | 每次 Dify 登录的 `DIFY_CONFIG_DIR`（含明文 `tokens.yml`，权限 0600） |
| `users/<account_id>/work/` | 该账号的上传文件、大结果和下载文件，保留 24 小时 |
| `home/` | difyctl 子进程的 `HOME` |

备份 `/opt/mcp-plugins/data/difyctl-mcp/state` 时按敏感数据处理。

## OAuth

本服务同时是 Authorization Server 和 Resource Server。MCP 令牌和 Dify 令牌（`dfoa_`）完全分开，Dify 令牌永远不发给客户端，所有输出都会脱敏 `dfoa_` 开头的字符串。

公开的端点（由部署器写入 `/etc/nginx/mcp-plugins.d/difyctl-mcp.conf`，只公开下列路径，不公开 `/health`）：

| 公开路径 | 用途 |
| --- | --- |
| `/difyctl-mcp/mcp` | MCP（未带令牌返回 401 和 `resource_metadata`） |
| `/.well-known/oauth-protected-resource/difyctl-mcp/mcp` | RFC 9728 资源元数据 |
| `/.well-known/oauth-authorization-server/difyctl-mcp` | RFC 8414 授权服务器元数据，issuer 为 `https://zendesk.smlershou.top/difyctl-mcp` |
| `/difyctl-mcp/authorize`、`/token`、`/register`、`/revoke` | 授权、换令牌、动态注册、撤销 |
| `/difyctl-mcp/login`、`/difyctl-mcp/login/start` | Dify 登录页（`?request=<id>`）及“继续”表单；均为精确路由 |

授权流程：

1. 客户端注册（只接受 `MCP_ALLOWED_REDIRECT_URIS` 中的回调），发起带 PKCE S256 的 `/authorize`。
2. `/authorize` 保存待授权记录，设置该记录专属的 `HttpOnly`、`Secure`、`SameSite=Lax` Cookie，并跳转到登录页。换一个浏览器打开登录页会被拒绝。
3. 登录页显示客户端名称、回调域名和申请的权限。用户点“继续”（POST + CSRF）后，服务创建新的配置目录并运行 `difyctl login --server <DIFY_SERVER> --no-browser --no-wait --no-keyring --json`。只显示位于 `DIFY_SERVER` 同源的验证页。
4. 页面显示验证码和验证页链接，每 5 秒自动刷新；服务端运行 `difyctl login --resume --json`，同一待授权记录 5 秒内最多一次。
5. 批准后运行 `describe account` 和 `get account session`，按 `current` 标记或 `device_label = difyctl-mcp` 取登录到期时间，找不到时按 14 天计算。
6. 邮箱不在 `MCP_ALLOWED_EMAILS` 时运行 `logout`（结束 Dify 会话）、删除目录并显示错误。
7. 通过后把账号的配置目录指向新目录、删除旧目录，生成一次性授权码并跳回客户端，客户端在 `/token` 换取令牌。

| 类型 | 有效期 |
| --- | --- |
| 授权码 | 5 分钟，一次性（PKCE 校验失败也会作废） |
| 访问令牌 | 1 小时，且不超过 Dify 登录到期时间 |
| 刷新令牌 | 不超过 Dify 登录到期时间，每次刷新轮换 |

- 令牌为 256 位随机数，数据库只存 SHA-256；客户端密钥同样只存哈希。
- 刷新令牌被重复使用时撤销该客户端在该账号下的全部令牌。
- Dify 登录到期（14 天，不能延长，无 refresh token）后 refresh 返回 `invalid_grant`，需要重新授权并在 Dify 再次批准。
- 工具遇到 `auth_expired` / `not_logged_in` 时撤销该账号全部 MCP 令牌，下一次请求收到 401。
- 同一账号从多个客户端授权时共用最新的 Dify 登录。
- 令牌只对本 MCP 有效：`resource` 参数必须是本服务，资源服务器校验令牌的 `resource`。
- `/register`、`/token`、`/revoke`、`/authorize`、`/login` 按客户端 IP 限流。

| Scope | 可调用的工具 |
| --- | --- |
| `difyctl:read` | read 工具 |
| `difyctl:write` | read、write 工具 |
| `difyctl:destructive` | 全部工具 |

客户端未申请 scope 时授予不超过 `DIFYCTL_MAX_EFFECT` 的全部权限（登录页会显示）。

部署器的冒烟检查使用 `DIFYCTL_MCP_API_KEY`：它只能列出工具，没有账号和 scope，任何工具调用都会被拒绝。

## 工具

| 工具 | 类型 | 说明 |
| --- | --- | --- |
| `difyctl_status` | read | 版本、账户、工作空间、登录到期时间；到期前 3 天和版本不一致时警告 |
| `difyctl_help` | read | `help` 的地图、命名空间、搜索和单条 descriptor，并标注 `mcp_tool` |
| `difyctl_guide` | read | 读取内置 Skill 文档及 CLI → MCP 的映射说明 |
| `difyctl_get_file` | read | 分段读取本账号工作目录里的文件 |
| `difyctl_refresh_catalog` | read | `refresh cache` 并清空 descriptor 缓存 |
| `difyctl_read` | read | 执行 read 操作 |
| `difyctl_write` | write | 执行 write 操作 |
| `difyctl_destructive` | destructive | 执行 destructive 操作 |
| `difyctl_put_file` | write | 把文件（≤25 MB，text 或 base64）放进本账号工作目录 |
| `difyctl_use_workspace` | write | `use workspace <id>` |
| `difyctl_logout` | destructive | `logout`，删除登录目录并撤销该账号全部 MCP 令牌 |

调用方式固定为 `DIFY_CONFIG_DIR=<该账号目录> difyctl <words> --input @- --json`：

- 账号由访问令牌的 subject 确定，调用方不能指定配置目录；不经过 shell，业务参数全部用 JSON 走 stdin。
- 操作名需匹配 `^[a-z0-9_]+([. ][a-z0-9_]+)*$`，且 `difyctl help <id> --json` 返回的 descriptor 必须带 `op`（本地命令如 `install skills`、`set config`、`login` 不能执行）。descriptor 缓存 5 分钟。
- 子进程环境变量只有 `PATH`、`HOME`、`DIFY_CONFIG_DIR`、`DIFY_OUTPUT=json` 和代理变量；不设置 `DIFY_TOKEN`、`DIFY_WORKSPACE_ID`。
- 同一账号的登录切换、`use workspace`、`logout` 与普通调用用读写锁隔开；锁为写者优先，持续的调用不能阻止 logout 或重新授权。

### 权限分类

等级 read < write < destructive，来自 descriptor 的 `effect`（缺失按 destructive）。操作等级高于工具等级、令牌 scope 或 `DIFYCTL_MAX_EFFECT` 时拒绝，并提示应使用的工具。

- 改成 read：`check.console_app.dsl`、`knowledge_fs.diff`
- 改成 destructive：`reset.*`、`restore.console_app.version`，以及所有 `delete.`、`remove.`、`revoke.` 前缀
- 禁止：`delete.account.current_session`（用 `difyctl_logout`）、任何 `include_secret` 非 false 的输入
- 不开放：`install skills`、`set/unset config`、`delete cache`、`--insecure` 及任何额外命令行参数

### 文件

`run.console_app.*`、`test.console_app.*`、`test.node.*`、`submit.run.form`、`upload.console_app.file` 等操作中 `bind=file` 的字段会被 difyctl 当作本地路径读取并上传。

- 先用 `difyctl_put_file` 上传，再在文件字段里写返回的相对路径。
- 写入前检查存储配额：每个账户工作目录默认最多 256 MiB、1000 个文件，所有账户合计默认 2 GiB（`DIFYCTL_MAX_ACCOUNT_STORAGE_MB`、`DIFYCTL_MAX_TOTAL_STORAGE_MB`）。超限立即拒绝；大结果超限时只返回预览，不落盘。
- 每个文件字段的值（路径、文件夹、列表、名称 → 路径映射）都用 realpath 校验必须位于本账号工作目录内；文件夹内不能有符号链接。子进程 cwd 为工作目录。
- difyctl 还会把对象、列表、JSON 字段中以 `@` 开头的字符串当文件读取（`@-` 为 stdin），这类值一律拒绝。
- MCP 请求体上限 36 MiB（25 MB 文件的 base64），Nginx 只对 `/mcp` 放宽到 40 MB。

### 结果

返回 `outcome`（`success` / `needs_attention` / `failed`）、`exit_code`、`result`、`error`、`next_step`、`notices`、`timed_out`。

- 需要关注：`status` 为 `suspended`/`incomplete`/`pending`/`failed`，`valid=false`，`ready=false`，`leaked_dependencies` 非空。
- 退出码 4 + `auth_expired`/`not_logged_in`：撤销 MCP 令牌并提示重新授权；4 + `access_denied`：只报告权限问题。
- 退出码 6：提示调用 `difyctl_refresh_catalog`；退出码 7：返回 `retry_after_seconds=60`。write 和 destructive 一律不自动重试。
- 结果超过 `DIFYCTL_MAX_OUTPUT_CHARS` 时存入工作目录 `outputs/`，返回前 2000 字符预览和路径。
- 超时（默认 90 秒，最长 600 秒）先发 SIGINT，5 秒后 SIGKILL（整个进程组）。流式运行被中断时返回 `status: incomplete` 和 `run_id`，之后用 `describe.run` 查询。`stop.run` 需要的 `task_id` 只在流式事件里，用 `stream=true`、`only=["workflow_started"]` 获取。
- 审计日志写 stdout：时间、账号、客户端、工具、操作、`app_id`、退出码、耗时，不记录 input。

后台每 5 分钟清理过期的待授权记录（及其配置目录）、授权码、令牌、孤立配置目录和超过 24 小时的工作文件。

## 配置

非敏感配置在 `deploy/runtime.json`，敏感值在 GitHub Actions Secrets：

| 变量 | 来源 | 说明 |
| --- | --- | --- |
| `MCP_PUBLIC_URL` | runtime.json | 对外地址，作为 issuer；资源为 `<MCP_PUBLIC_URL>/mcp` |
| `MCP_ALLOWED_REDIRECT_URIS` | runtime.json | 允许的回调地址（空格或逗号分隔）。`http://localhost/callback` 这类不带端口的 loopback 地址按 RFC 8252 匹配任意端口 |
| `MCP_ALLOWED_EMAILS` | Secret `MCP_DIFYCTL_MCP__MCP_ALLOWED_EMAILS`（必填） | 允许登录的 Dify 邮箱，默认拒绝 |
| `DIFYCTL_MCP_API_KEY` | Secret `MCP_DIFYCTL_MCP__DIFYCTL_MCP_API_KEY`（必填） | 部署冒烟检查用，只能列出工具 |
| `MCP_STATIC_CLIENT_ID`、`MCP_STATIC_CLIENT_SECRET` | Secret（可选） | 不支持动态注册的客户端使用的预注册 client；回调地址同 `MCP_ALLOWED_REDIRECT_URIS` |
| `DIFY_SERVER` | runtime.json | 默认 `https://cloud.dify.ai` |
| `DIFYCTL_MAX_EFFECT` | runtime.json | 默认 `destructive`；更低时不注册更高等级的工具，也不授予对应 scope |
| `DIFYCTL_TIMEOUT_SECONDS`、`DIFYCTL_MAX_TIMEOUT_SECONDS` | runtime.json | 默认 90、600 |
| `DIFYCTL_MAX_OUTPUT_CHARS` | runtime.json | 默认 100000 |
| `DIFYCTL_MAX_CONCURRENT`、`DIFYCTL_MAX_ACCOUNT_CONCURRENT` | 可选环境变量 | 全局和单账户同时运行的 difyctl 进程上限，默认 16、4；超限立即返回 `busy` 和 `retry_after_seconds`，不排队 |

升级 difyctl：修改 Dockerfile 的 `DIFY_VERSION` 和 `DIFYCTL_SHA256`（取自新 release 的 checksums 文件），重新构建；数据卷保留登录信息。升级后要回归登录流程（依赖 `login --no-wait` 和 `--resume`）。

## 开发

```bash
uv sync --locked --all-extras --all-groups
uv run --locked pytest -q
```

测试用 `tests/fake_difyctl.py` 模拟 difyctl 1.18 的 JSON 输出、退出码和登录状态，覆盖设计文档第 14 节的验收项。真实 difyctl 的行为以 1.18.0 二进制源码核对。

## 风险和未验证项

- 每 14 天必须人工重新批准一次，这是 Dify 的限制。
- 以下行为来自 difyctl 源码，未在真实账号上验证：Dify 返回 `slow_down` 时 `--resume` 仍按 `pending` 处理（本服务固定 5 秒检查一次）；超时 SIGINT 后输出 `incomplete` 和 `run_id`；`get account session` 的字段名及能否按 `device_label` 匹配当前会话（匹配不到时按 14 天估算，访问令牌仍不超过该时间）。
- 首次部署前需要管理员用 `python3 deploy/install_server.py --update-helper-only` 更新服务器执行器（本服务依赖部署器新增的公开路径、固定主机名和数据目录属主支持）。
