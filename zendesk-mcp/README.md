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

Zendesk 将在 2027-04-30 停用 Support API token。生产环境使用 `ZENDESK_AUTH_MODE=oauth`，凭据格式与团队 `zd auth login`（zendesk-cli 团队版，PKCE 授权码 + 轮换 refresh token）生成的 JSON 文件一致：

- `ZENDESK_OAUTH_SEED_FILE`：只读种子，部署时由 GitHub Secret `MCP_ZENDESK_MCP__ZENDESK_OAUTH_SEED` 以文件挂载到 `/run/mcp-secrets/zendesk_oauth_seed.json`。
- `ZENDESK_OAUTH_FILE`：可写的私有凭据文件，位于持久化目录 `/var/lib/mcp/oauth/credentials.json`（服务器 `/opt/mcp-plugins/data/zendesk-mcp/oauth/`）。每次刷新都会轮换 refresh token 并写回此文件。
- 首次使用时把种子复制到凭据文件；之后重新部署若种子内容未变，不会覆盖已轮换的 token；种子内容变化（重新授权）时替换凭据文件。
- 后台每小时检查一次，凭据超过 `ZENDESK_OAUTH_KEEPALIVE_HOURS`（默认 24）未刷新即主动刷新，避免空闲 30 天后 refresh token 过期。
- 访问令牌只发送到 `https://<子域名>.zendesk.com`，附件 CDN 等其他主机不会收到。遇到 401 会强制刷新一次再重试。
- `/health` 返回 `zendesk_auth_mode` 和 `zendesk_oauth`（是否有凭据、授权用户 ID、令牌是否有效、上次刷新距今秒数、最近错误），不含任何令牌值。
- 可选 `ZENDESK_OAUTH_EXPECTED_USER_ID`：设置后拒绝导入其他 Zendesk 用户的种子。

所有工单修改都以授权人的 Zendesk 身份记录。

### 生成或更换种子

1. 在自己电脑上用团队 CLI 授权到一个**专用**文件（不要复用日常 CLI 的文件，两边互相轮换会使对方失效）：

   ```bash
   zd auth login --subdomain langgenius --client-id dify_zendesk_cli \
     --expected-user-id <自己的数字用户 ID> --file "$HOME/.zendesk-cli/oauth-zendesk-mcp.json"
   ```

2. 立刻把该文件完整内容保存为 GitHub Secret `MCP_ZENDESK_MCP__ZENDESK_OAUTH_SEED`。之后**不要**再用这个文件运行 `zd`（会轮换掉种子里的 refresh token），确认部署成功后删除本地文件。
3. 手动运行工作流并指定服务 `zendesk-mcp`。种子需在授权后 30 天内首次使用。
4. 部署后检查 `/health` 中 `zendesk_oauth.credentials` 为 `true`、`last_refresh_age_seconds` 有值且没有 `last_error`，再调用一个只读工具确认。

需要临时回退到 API token 时，把 `deploy/runtime.json` 中 `ZENDESK_AUTH_MODE` 改为 `api_token` 并部署；注意 Zendesk 会自动停用连续 30 天未使用的 API token。

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
