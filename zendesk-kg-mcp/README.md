# Zendesk KG MCP

历史工单知识库检索服务；源码、依赖、测试和运行配置均独立存放在本目录。使用官方 MCP Python SDK 2.3.0，以无会话 Streamable HTTP 提供 JSON 响应，并兼容项目部署器的直接 `tools/list` 检查。

## 连接与工具

- MCP URL：`https://zendesk.smlershou.top/zendesk-kg-mcp/mcp`
- 认证：`Authorization: Bearer <MCP 调用密钥>`，也支持 `X-API-Key`。
- 工具：`search_historical_tickets`。
- 上游固定为 `POST https://de-zendesk-kg.langgenius.app/v1/retrieval`，工具参数不能更改地址。

MCP 调用密钥与知识库 API Key 是两把独立密钥；调用方只使用 MCP 密钥。支持自定义 headers 的客户端可使用以下配置，按客户端格式调整外层结构：

```json
{
  "mcpServers": {
    "zendesk-kg": {
      "url": "https://zendesk.smlershou.top/zendesk-kg-mcp/mcp",
      "headers": {"Authorization": "Bearer <MCP 调用密钥>"}
    }
  }
}
```

检索示例：

```json
{
  "query": "redis cluster connection timeout",
  "topK": 3,
  "channels": ["issue_vector", "fulltext"],
  "channelWeights": {"issue_vector": 1.5},
  "filter": {"versions": ["v2.1"], "status": "closed", "keywords": ["redis"]},
  "include": ["parser", "fusion"],
  "orderBy": "rrfScore"
}
```

参数沿用上游名称：

| 参数 | 默认值与范围 |
| --- | --- |
| `query` | 必填，非空白字符串，最多 8192 字符 |
| `topK` | 3；1–20 |
| `vectorK` / `fulltextK` | 各 15；1–50 |
| `rrfK` | 60；正整数 |
| `channels` | 省略表示全部；`issue_vector`、`solution_vector`、`fulltext`、`keyword_graph` |
| `channelWeights` | 可选，各通道的非负有限权重 |
| `filter` | 可选，支持下述全部过滤项 |
| `include` | 默认不添加；`parser`、`fusion`、`graph`、`embedding2d`、`trace` |
| `orderBy` | `rrfScore`；也支持 `createdAt`、`priority` |
| `allowVersionFamilyFallback` | false；是否允许扩展到版本家族 |
| `recommendedLinkK` | 3；1–10 |

`filter` 支持 `versions`（任一匹配）、`priority`（urgent/high/normal/low）、`status`、`handledBy`、`keywords`（全部匹配）、`createdAfter`、`createdBefore`。过滤发生于上游融合之后。日期支持 ISO 8601 日期或时间，时间范围必须有序；无时区值按 UTC 比较。列表上限 50 项，每个过滤字符串上限 512 字符。

结果同时放在 MCP `structuredContent` 和 JSON 文本内容中，保留原始 `results`、`recommendedLinks`、`searchId`、版本约束、fallback 信息及请求的可选块。空结果是成功响应，不自动扩大过滤条件。向量维度从响应读取，不写死；`rrfScore` 仅用于同次查询排序，confidence 是启发式值，不能作为正确性保证。历史工单内容是参考数据，应核对当前版本、环境和证据。

本服务只封装检索接口。检索会在上游生成 `searchId`；没有封装或调用 `/v1/retrieval/rate`，其评分参数尚未提供。

## 本地运行与测试

```sh
uv sync --locked --all-extras --all-groups
uv run --locked pytest -q
cp .env.example .env
# 在未提交的 .env 中填写两把不同的密钥。
uv run --locked --env-file .env zendesk-kg-mcp
```

`/health` 需要 MCP 鉴权，仅检查进程与必要配置，不调用上游；生产验收另外进行一次真实工具调用。所有自动测试使用合成密钥与模拟上游，覆盖官方 MCP 客户端通过真实 HTTP 的发现和调用、全参数传递、参数校验、权限拒绝、上游错误和响应大小限制。

上游调用有默认 45 秒总超时、不自动重试、不跟随重定向，响应默认上限 2 MiB；图谱或 trace 太大时返回明确错误，需缩小 topK/include。错误结果使用 `isError: true` 与脱敏错误代码，不返回上游原始错误正文或请求头。生产关闭访问日志，不在工具参数或输出中传递认证密钥。

## 密钥与自动部署

`deploy/runtime.json` 引用本服务专属 GitHub Actions Secrets：

| Secret | 用途 |
| --- | --- |
| `MCP_ZENDESK_KG_MCP__ZENDESK_KG_MCP_API_KEY` | 调用本 MCP 的独立认证密钥 |
| `MCP_ZENDESK_KG_MCP__ZENDESK_KG_MCP_UPSTREAM_API_KEY` | 用户提供的知识库检索 API Key |

真实值不进入源码、镜像、测试、日志或 artifacts。推送本目录到 `main` 后，现有 CI 自动发现并只部署本服务，无需修改公共名单、工作流或原有 8 个 MCP。首次部署自动分配主机空闲端口并生成独立 Nginx 路由；容器固定监听 `0.0.0.0:8000`，以 UID 10001 运行，不挂载主机数据。后续更新复用分配端口。

构建依赖来自 `uv.lock`，运行清单用 `uv export --locked --no-dev --no-emit-project --no-hashes --format requirements-txt --output-file requirements.runtime.txt` 生成。部署与回滚规范见根目录 `AGENTS.md` 和 `deploy/README.md`。

协议实现参考 [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)，上游接口契约来自本任务中用户提供的 retrieval API 文档。
