# docs-tool-service

从腾讯云当前 `docs-tool-service` 运行容器整理的独立 MCP 项目。端口：`8112`。全部业务代码位于本目录 `src/docs_tool_service/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --all-extras --all-groups
uv run --locked pytest -q
cp .env.example .env
# 填写本地凭证后启动
uv run --locked docs-tool-service
```

使用 Python 3.12。

## 工具与数据来源

| 工具 | 数据来源 | 说明 |
| --- | --- | --- |
| `official_docs_search` | 社区文档 `docs.dify.ai`（`llms-full.txt` 英文全文 + `_llms/zh.md`、`_llms/ja.md` 索引页）；企业文档 `enterprise-docs.dify.ai`（`llms-full.txt` 英文当前版本 + `_llms/{zh,ja}/3-12-x.md` 索引页；指定版本时按需加载 `_llms/{语言}/{版本}.txt`） | 全文检索，参数 `query`、`source`、`language`、`version`、`edition`、`limit` |
| `official_docs_read` | 上述两个文档站点，优先读取页面 `.md` | 分段读取，返回 `title`、`language`、`version`、`edition` |
| `official_site_list` / `official_site_search` | `dify.ai/sitemap-index.xml` | 官网页面列表 / 按 URL 与标题查找，支持 `language`（auto 表示不过滤）和 `section` |
| `official_site_read` | `dify.ai` 官网 HTML | 分段读取 |

所有工具只访问 Dify 官方域名，不使用 Zendesk Help Center 或第三方搜索。

### `official_docs_search` 参数

- `source`：`auto`（默认，社区 + 企业）、`official_docs_community`（别名 `community`、`cloud`）、`official_docs_enterprise`（别名 `enterprise`）。
- `language`：`auto`（默认）、`all`、`en`、`zh`、`ja`。`auto` 按查询文字推断：含假名为 `ja`，含汉字为 `zh`，否则为 `en`；若该语言无结果，自动退回全部语言并在 `notices` 中说明。只含汉字的日文查询会被识别为 `zh`，此时请显式传 `ja`。注意与 `official_site_*` 不同，那里的 `auto` 表示不过滤。
- `version`：企业文档版本族，接受 `3.5`、`3.5.x`、`v3.5.2`、`3-5-x` 或 `latest`，统一为 `3.5.x`；默认空表示不过滤。
  - 实现方式：企业文档页面 URL 带版本段（`/en/3.5.x/...`），每个版本另有独立索引 `_llms/{语言}/{版本}.txt`。默认语料只含英文最新版及 zh/ja 3.12.x；请求其他版本时按需抓取该版本索引下的全部页面（首次约数秒到十几秒），按 (版本, 语言) 缓存至少 6 小时、最多 8 份，再按 URL 版本段过滤。
  - 可用版本取自 `enterprise-docs.dify.ai/llms.txt`，指定版本时在 `available_versions` 返回。版本不存在、某语言未发布该版本（如 ja 2.5.x）或无匹配页面时，`results` 为空并在 `notices` 写明原因。
  - 社区文档只发布最新版、无版本信息；版本过滤生效时排除社区文档并在 `notices` 说明。
  - 未传 `version` 时会识别查询中的版本号：只搜企业文档时任何 `主.次` 数字都算；`source=auto` 时只识别 `3.5.x`、`v3.5` 这类明确写法，避免 "python 3.12" 误过滤。识别出的版本会从排序用的查询词中去掉，`filters.version_origin` 为 `query`。
- `edition`：`all`（默认）、`cloud`、`self-hosted`（也接受 `self-host`）。判断依据和局限：
  - 社区文档主要依据 URL 段：`/{语言}/cloud/...` 为 Cloud，`/{语言}/self-host/...` 为自托管；
  - API 参考、插件开发、CLI、learn、quick-start 等两边共用的页面标为 `shared`，两种过滤都保留；若正文含 "only available on Dify Cloud"、"self-hosted only"、"仅适用于自部署" 等明确标记，则归入对应版本（当前官方语料中几乎没有此类标记）；
  - 页面 `.md` 没有 frontmatter 等元数据可用；
  - 企业文档标为 `enterprise`，视为私有部署：`cloud` 时排除，`self-hosted` 时保留。
  - `cloud` 不是硬过滤：`shared` 页面仍会返回，但 Cloud 专属页面排在命中同样多查询词的 `shared` 页面之前（见下方排序规则第 3 条）。`all`、`self-hosted` 的排序不变。
- 每条结果包含 `title`、`url`、`description`（摘要；OpenAPI 生成的 API 参考页固定为接口的 `summary — description`，其他页面为围绕查询词的正文片段）、`source`、`language`、`version`（版本族，社区文档为 `null`）、`edition`、`matched_terms`。响应另含 `filters`（实际生效的过滤条件）、`notices`、`indexed_pages`（已加载页数）、`searched_pages`（过滤后参与检索的页数）。

### 排序规则

查询会先去掉常见停用词（how、to、the、如何 等）再拆分为查询词，按以下顺序排序：

1. 命中全部查询词的页面优先，其后按命中词数排序；
2. 同等命中数按加权分：标题命中 > heading 命中 > URL 路径命中 > 正文命中（正文出现次数有上限）；每个词按逆文档频率加权，多数页面都有的常见词（如 workflow）权重低，罕见词（如 timeout）权重高；完整短语出现在标题、heading、正文时额外加分；
3. `edition=cloud` 时，Cloud 专属页面视为每个命中的查询词额外命中一个权重为 `CLOUD_EDITION_BOOST`（`tools.py` 常量，默认 5.0；对比标题 12、heading 8、URL 6、正文 2–5）的字段，同样乘以该词的逆文档频率。它只在命中词数相同的页面之间调整顺序，不会让页面越过命中更多查询词的页面；设为 0 即关闭。例如 `知识库 文档上传 大小限制` 在 `edition=cloud` 下，「知识库存储与文档配额」排在共用的「Agent 知识库 API」之前；
4. 英文按词前缀匹配（`timeout` 可匹配 `timeouts`，`api` 不匹配 `rapid`）；中日文按子串匹配，连续中文不切词，多个关键词建议用空格分隔。

摘要选取覆盖最多查询词、并以最罕见词为中心的片段。

### 内容清洗与标题

- 建索引及 `official_docs_read` 返回前会去掉 llms 的 "Documentation Index" 说明块、Mintlify 页脚（每个页面 `.md` 末尾的 "This documentation is built and hosted on Mintlify…"，以及 "Powered by Mintlify"、"Was this page helpful?"、"Suggest edits" 等整行站点文字；正文中提到 Mintlify 的句子保留）、markdown 图片 `![..](..)`、指向图片的链接（保留链接文字）、图片引用定义、HTML `<img>`、空的 `<Frame>` 以及裸图片 URL（png/jpg/gif/webp/svg 等）；非图片链接保留。
- 仅建索引时（`official_docs_read` 保留原文）还会：
  - 去掉 zh/ja 页面开头的 AI 翻译提示（"本文档由 AI 自动翻译…"、"このドキュメントは AI によって自動翻訳されています…"）；
  - 把 API 参考页内嵌的整份 OpenAPI YAML（`## OpenAPI` 加 ````` ````yaml <spec> <method> <path> ````` 代码块）换成可读文本：`METHOD /path`、`summary`、`description`，以及参数、请求体字段（跟随 `#/components/...` 的 `$ref`）和响应码的说明；每页重复的 `info`、`servers`、`tags`、`components` 等整份规范内容不再进入索引，避免无关查询命中所有 API 页；
  - 英文 `llms-full.txt` 中的规范引用行（`/en/api-reference/openapi_service.json post /apps/annotations`）改为 `POST /apps/annotations`。
- 索引在运行时由官方 feed 抓取生成（缓存至少 6 小时），仓库中没有预构建索引，无需重新生成；部署（进程重启）后首次搜索即按新规则建索引。
- `official_docs_read` 的标题依次取 frontmatter `title`、`<title>`、第一个 H1（markdown `# `，跳过代码块；或 HTML `<h1>`）。

## 审计日志

每次 `tools/call`（包括未知工具和参数格式错误）向 stdout 输出一行 JSON，日志器为 `docs_tool_service.audit`，格式与 difyctl-mcp 的审计日志一致：

```json
{"time": "2026-11-11T08:00:00Z", "tool": "official_docs_search", "duration_ms": 412, "outcome": "success", "error": null, "response_chars": 5120, "args": {"edition": "cloud", "language": "zh", "limit": 5, "query": null}}
```

- `time` 为 UTC；`outcome` 为 `success` 或 `failed`；`error` 为工具错误码（如 `BAD_ARGUMENT`、`OFFICIAL_CONTENT_HTTP_ERROR`）、JSON-RPC 错误码（未知工具为 `-32601`）或 `null`；处理器异常时为 `INTERNAL_ERROR`；`response_chars` 为返回文本长度。
- `args` 列出全部参数名；只有 `source`、`language`、`version`、`edition`、`section`、`limit`、`offset`、`max_chars`、`cursor` 这类非敏感参数在值为不超过 64 字符的标量时记录值，其余（包括 `query`、`url`）值一律为 `null`，不记录搜索内容。
- `main_docs()` 启动时为该日志器单独挂 stdout handler（INFO 级、不向 root 传播），不受 uvicorn 日志配置影响，也不会把 httpx 等其他库的 INFO 日志带到 stdout。uvicorn 自身的启动日志仍在 stderr，访问日志仍在 stdout。
- 写日志失败不会影响工具调用结果。

所有工具都在 `tools/list` 中声明 `annotations.readOnlyHint: true`、`destructiveHint: false`。OpenConnector 等客户端据此判断读写，未声明时新工具会被当作 write 并要求确认。

## 构建

```bash
docker build -t docs-tool-service:local .
```

Dockerfile 使用本目录完整源码和容器实际依赖版本，不依赖服务器上的旧镜像标签。生产配置、API 密钥、浏览器资料和业务数据应单独挂载，不能放入构建上下文。

## 版本来源

- 原镜像：`tool-services:latest`。
- 镜像 ID：`sha256:293a6a806085797799cafdaa269ed0cce2b03a37471fe10ce9556dfee95d4f21`。
- `baseline/runtime.json` 记录实际依赖与容器来源；`baseline/source-manifest.json` 记录原始文件哈希。
- `requirements.runtime.txt` 固定运行依赖；`pyproject.toml` 通过 constraints 保持同样版本。原锁文件和部署文本保存在 `baseline/`，不作为当前部署入口。
- 从该服务自己的线上 tool_services 快照拆出独立包；只调整包名、服务装配及配置字段，保留其业务实现和 MCP 传输行为。
