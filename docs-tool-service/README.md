# docs-tool-service

从腾讯云当前 `docs-tool-service` 运行容器整理的独立 MCP 项目。端口：`8112`。全部业务代码位于本目录 `src/docs_tool_service/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest
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
- 每条结果包含 `title`、`url`、`description`（摘要）、`source`、`language`、`version`（版本族，社区文档为 `null`）、`edition`、`matched_terms`。响应另含 `filters`（实际生效的过滤条件）、`notices`、`indexed_pages`（已加载页数）、`searched_pages`（过滤后参与检索的页数）。

### 排序规则

查询会先去掉常见停用词（how、to、the、如何 等）再拆分为查询词，按以下顺序排序：

1. 命中全部查询词的页面优先，其后按命中词数排序；
2. 同等命中数按加权分：标题命中 > heading 命中 > URL 路径命中 > 正文命中（正文出现次数有上限）；每个词按逆文档频率加权，多数页面都有的常见词（如 workflow）权重低，罕见词（如 timeout）权重高；完整短语出现在标题、heading、正文时额外加分；
3. 英文按词前缀匹配（`timeout` 可匹配 `timeouts`，`api` 不匹配 `rapid`）；中日文按子串匹配，连续中文不切词，多个关键词建议用空格分隔。

摘要选取覆盖最多查询词、并以最罕见词为中心的片段。

### 内容清洗与标题

- 建索引及 `official_docs_read` 返回前会去掉 llms 的 "Documentation Index" 说明块、markdown 图片 `![..](..)`、指向图片的链接（保留链接文字）、图片引用定义、HTML `<img>`、空的 `<Frame>` 以及裸图片 URL（png/jpg/gif/webp/svg 等）；非图片链接保留。
- `official_docs_read` 的标题依次取 frontmatter `title`、`<title>`、第一个 H1（markdown `# `，跳过代码块；或 HTML `<h1>`）。

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
