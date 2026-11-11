# zendesk-mcp

从腾讯云当前 `zendesk-mcp` 运行容器整理的独立 MCP 项目。端口：`8100`。全部业务代码位于本目录 `src/zendesk_mcp/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest
cp .env.example .env
# 填写本地凭证后启动
uv run --locked zendesk-mcp
```

使用 Python 3.12。

## Zendesk 认证（OAuth）

Zendesk 将在 2027-04-30 停用 Support API token。生产环境使用 `ZENDESK_AUTH_MODE=oauth`，授权流程内置在 MCP 中，只需在首次部署后登录一次：

1. 调用工具 `zendesk_auth_start`，得到 Zendesk 授权链接（10 分钟内有效）。
2. 在浏览器打开链接，用本 MCP 要代表的 Zendesk 账号登录、完成双重验证并允许访问。
3. 浏览器会跳到 `http://127.0.0.1:8765/callback?code=...&state=...`，页面打不开属于正常现象。复制地址栏中的完整地址。
4. 调用 `zendesk_auth_complete`，参数 `callback_url` 填入该地址。成功后返回授权的用户 ID 和姓名。
5. 用 `zendesk_auth_status` 或 `/health` 确认 `authorized` 为 `true`。

实现说明：

- 使用团队公开 PKCE 客户端 `dify_zendesk_cli`（回调地址 `http://127.0.0.1:8765/callback`，与 `zd auth login` 相同），不需要 client secret、CLI 或 GitHub Secret。PKCE 校验码只保存在服务器，复制出来的授权码一次性、几分钟内过期，单独泄露也无法换取令牌。
- 申请的 scope 仅限工具所需：`read tickets:read tickets:write users:read hc:read ticket_views:read groups:read ticket_attachments:read`（可用 `ZENDESK_OAUTH_SCOPE` 覆盖）。
- 令牌保存在持久化目录 `/var/lib/mcp/oauth/credentials.json`（服务器 `/opt/mcp-plugins/data/zendesk-mcp/oauth/`，权限 0600）。每次刷新都轮换 refresh token；重新部署不影响已保存的授权。
- 后台每小时检查一次，超过 `ZENDESK_OAUTH_KEEPALIVE_HOURS`（默认 24）未刷新即主动刷新，避免空闲 30 天后 refresh token 过期。授权失效时，Zendesk 工具会返回"请调用 `zendesk_auth_start`"的错误，重新登录即可。
- 首次授权的 Zendesk 用户会被锁定，之后重新授权必须是同一用户；确需更换时设置 `ZENDESK_OAUTH_EXPECTED_USER_ID` 为新用户 ID 并部署。每次成功授权都写入审计日志（只记录用户 ID）。
- 访问令牌只发送到 `https://<子域名>.zendesk.com`，附件 CDN 等其他主机不会收到；遇到 401 会强制刷新一次再重试。工具结果、日志和 `/health` 不包含任何令牌、授权码或校验码。

所有工单修改都以授权人的 Zendesk 身份记录。

OAuth 模式不读取 `ZENDESK_API_TOKEN`，也不需要 `ZENDESK_EMAIL`；`deploy/runtime.json` 已不再映射 `ZENDESK_API_TOKEN`。`/health` 的 `zendesk_configured` 只取决于 OAuth 授权状态。需要临时回退到 API token 时，把 `deploy/runtime.json` 中 `ZENDESK_AUTH_MODE` 改为 `api_token`，在 `secrets` 中重新加入 `ZENDESK_API_TOKEN`（GitHub Secret `MCP_ZENDESK_MCP__ZENDESK_API_TOKEN`）并部署；注意 Zendesk 会自动停用连续 30 天未使用的 API token。

## 工具注解与禁用工具

`tools/list` 为每个工具返回 MCP `annotations`（`readOnlyHint`、`destructiveHint`、`idempotentHint`、`openWorldHint`）。只读工具集合唯一定义在 `src/zendesk_mcp/mcp_schema.py` 的 `READ_ONLY_TOOL_NAMES`，包括 `zendesk_auth_status`；`add_internal_note`、`update_ticket_status`、`update_ticket_metadata` 及 `zendesk_auth_start`、`zendesk_auth_complete` 标记为非只读。

`ZENDESK_MCP_DISABLED_TOOLS` 填写逗号分隔的工具名（允许空格，留空表示不禁用）。名称可带或不带 `zendesk_` 前缀，例如 `auth_start` 与 `zendesk_auth_start` 等价；无法识别的名称会被忽略。被禁用的工具不会出现在 `tools/list` 中，调用时返回与不存在的工具相同的 `unknown tool` 错误。生产配置当前禁用 `search_help_center,get_article`。

## 审计日志

工单写操作和 OAuth 授权写入 `ZENDESK_MCP_AUDIT_LOG`（JSON Lines）。生产路径为 `/var/lib/mcp/logs/audit.jsonl`，对应持久化挂载 `@data/logs` → `/var/lib/mcp/logs`（服务器 `/opt/mcp-plugins/data/zendesk-mcp/logs/`），重新部署不会丢失。父目录不存在时自动创建；镜像以 root 运行，可写入部署器创建的 root 0700 目录。

## 构建

```bash
docker build -t zendesk-mcp:local .
```

Dockerfile 使用本目录完整源码和容器实际依赖版本，不依赖服务器上的旧镜像标签。生产配置、API 密钥、浏览器资料和业务数据应单独挂载，不能放入构建上下文。

## 版本来源

- 原镜像：`zendesk-mcp:accept-fix-20261012`。
- 镜像 ID：`sha256:08cf136ea5767039e6b7b7bcf3712793295f126b37817d8a2d28007088e682f3`。
- `baseline/runtime.json` 记录实际依赖与容器来源；`baseline/source-manifest.json` 记录原始文件哈希。
- `requirements.runtime.txt` 固定运行依赖；`pyproject.toml` 通过 constraints 保持同样版本。原锁文件和部署文本保存在 `baseline/`，不作为当前部署入口。
- 原功能说明见 [baseline/README.original.md](baseline/README.original.md)。
