# DB Gateway

独立的数据库访问小服务，负责 Teleport DB proxy、证书续期、只读 SQL HTTP API，以及可供 agent 远程调用的 MCP HTTP endpoint。

## 目录职责

- `src/db_gateway/`: HTTP API、MCP HTTP endpoint、Teleport 认证页、证书续期依赖、只读 SQL 执行逻辑。
- `scripts/db_gateway_mcp.py`: 历史 stdio client，当前 Codex 接入不使用。
- `scripts/auth-watchdog.py`: Teleport 证书检查和自动续期/飞书提醒。
- `deploy/systemd/`: 服务器 systemd 单元，生产配置挂载 `/opt/db-gateway/config.yaml`。
- `deploy/nginx/`: 对外域名反代片段。

## Agent 接入

当前只使用远程 MCP：

```bash
URL=https://<db-gateway-domain>/db-gateway/mcp
Authorization=Bearer <shared-api-key>
```

Teleport proxy 等非密钥运行配置放在 `/opt/db-gateway/config.yaml`；数据库密码和续期凭证只放在 DB Gateway 服务自己的 `.env` 里，不再放进 agent 项目。
