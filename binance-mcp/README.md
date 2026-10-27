# Binance Spot 只读 MCP

独立 Python 3.12 服务，固定连接 Binance 官方 `https://api.binance.com` Spot REST API；只使用 GET，不提供下单、撤单、转账、提现或任意 URL/凭据工具。公开行情与账户数据均有 Binance 地域限制、权重及限流，429/418 等错误不会自动重试。账户查询使用本机时钟和 5000ms recvWindow；时钟偏差可能导致签名被拒绝。账户历史受 Binance 数据保留规则约束，并非完整归档。

## 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `exchange_info` | `symbol` | 指定交易对信息 |
| `price` | `symbol` | 最新价格 |
| `ticker_24h` | `symbol` | 24 小时统计 |
| `depth` | `symbol`, `limit` | 盘口，limit 为 5/10/20/50/100/500/1000/5000，默认 100 |
| `klines` | `symbol`, `interval`, `limit` | K线；limit 1–1000，默认 500；interval 见工具 schema |
| `account` | 无 | 账户信息及余额 |
| `open_orders` | `symbol` | 当前订单 |
| `all_orders` | `symbol`, `limit` | 历史订单，limit 1–1000，默认 500 |

交易对必须是 2–20 位大写英文字母或数字（如 `BTCUSDT`）；所有参数均严格校验，不接受其他参数。返回 JSON-RPC MCP 内容，失败为 `isError` 和通用错误码；上游失败时附带 `http_status` 与 Binance 数字错误码 `binance_code`（如 `-1021` 时钟偏差、`-2015` Key/IP 白名单/权限），常见错误附带排查提示，但不返回上游错误原文、密钥或签名 URL。账户数据本身敏感，MCP 调用者应妥善保护响应。

## 本地运行

```sh
uv sync --locked --all-extras --all-groups
# 在安全环境中设置 BINANCE_MCP_API_KEY；可选设置 BINANCE_API_KEY 和 BINANCE_API_SECRET
uv run --locked binance-mcp
uv run --locked pytest -q
```

`BINANCE_MCP_API_KEY` 为必需的 MCP Bearer 鉴权密钥；`BINANCE_API_KEY` 和 `BINANCE_API_SECRET` 均可选，但账户工具要求二者同时存在，不配置时公开行情仍可用。建议使用仅具读取权限、禁用交易和提现且有 IP 限制的 Binance API key，切勿提交凭据。`.env.example` 仅作变量说明，服务不会自动加载该文件。本地默认监听 `127.0.0.1:8124`（容器与 `deploy/runtime.json` 显式设置 `BINANCE_MCP_HOST=0.0.0.0`），可用 `BINANCE_MCP_HOST` / `BINANCE_MCP_PORT` 调整；`GET /health` 返回 JSON，`POST /mcp` 使用 `Authorization: Bearer <MCP密钥>`。应用层限制 HTTP 请求体上限（`BINANCE_MCP_MAX_BODY_BYTES`，默认 1,000,000 字节）与 JSON-RPC batch 批处理条目上限（`BINANCE_MCP_MAX_BATCH_SIZE`，默认 50 条），超限请求将被尽早拒绝。应在可信环境或 HTTPS 反向代理后使用；MCP 认证密钥不同于 Binance 凭据。

`deploy/runtime.json` 按仓库契约声明必需 MCP Secret 和两个可选 Binance Secret，固定容器端口 8124、主机端口自动分配。容器冒烟测试只需合成 MCP 密钥，不需 Binance 凭据或联网查询行情。此目录仅供本地开发，不代表已配置 Secrets 或部署。
