# github-mcp

从腾讯云当前 `github-mcp` 运行容器整理的独立 MCP 项目。端口：`8111`。全部业务代码位于本目录 `src/github_mcp/`，不依赖兄弟目录。

## 工具

| 工具 | 用途 | 主要参数 |
| --- | --- | --- |
| `github_search_prs` | 在指定仓库搜索 PR | `repo`、`query`、`limit`（≤10）、`max_body_chars`（默认 300） |
| `github_get_pr` | 读取单个 PR 的关键字段 | `repo`、`pr_number`、`max_body_chars`（默认 8000） |
| `github_get_pr_files` | PR 变更文件列表 | `repo`、`pr_number`、`page`、`per_page`（≤100）、`include_patch`、`max_patch_chars`（默认 2000） |
| `github_list_pr_commits` | PR 提交列表（sha、提交信息首行、作者、时间） | `repo`、`pr_number`、`page`、`per_page` |
| `github_compare` | 比较 `base...head`（分支、tag、SHA；fork 用 `owner:branch`） | `repo`、`base`、`head`、`max_commits`（默认 50）、`max_files`（默认 100）、`include_patch`、`max_patch_chars` |
| `github_search_issues` | 在指定仓库搜索 issue | `repo`、`query`、`limit`（≤10）、`max_body_chars` |
| `github_get_issue` | 读取 issue（或 PR 会话），默认附带第一页评论 | `repo`、`issue_number`、`include_comments`（默认 true）、`comments_page`、`comments_per_page`、`max_body_chars`、`comment_max_body_chars` |
| `github_list_issue_comments` | 分页读取 issue/PR 会话评论（不含 PR 行内 review 评论） | `repo`、`issue_number`、`page`、`per_page`、`max_body_chars`（默认 2000） |
| `github_list_releases` | Release 列表（新到旧） | `repo`、`page`、`per_page`（默认 10）、`max_body_chars`（默认 1000） |
| `github_get_release` | 按 tag 读取 release；省略 `tag` 时读取最新正式版 | `repo`、`tag`、`max_body_chars` |
| `github_code_search` | GitHub 代码搜索（仅默认分支） | `query`、`repo`、`scope`（默认 `langgenius`）、`path`、`language`、`visibility`、`limit`（≤30） |

### 返回格式

- 默认只返回关键字段：去掉 `*_url`（保留 `html_url`），用户折叠为 `login`，仓库折叠为 `full_name`，标签只保留名称。例如 PR 返回 `number`、`title`、`state`、`draft`、`merged`、`merged_at`、`user`、`base`/`head`（`ref`、`sha`、`repo`）、`labels`、`assignees`、`requested_reviewers`、时间戳、`body`、`html_url`、`additions`/`deletions`/`changed_files`、`comments`/`review_comments`、`mergeable_state` 等。
- 长文本按 `max_body_chars`/`max_patch_chars` 截断，截断时附带 `<字段>_truncated: true` 与原始长度 `<字段>_chars`；设为 `0` 时不返回正文。
- 所有工具支持 `fields`：字段路径列表（如 `["head.sha", "labels.name"]`），从原始对象中取值，结果以路径为键；路径遇到数组时逐项取值，不存在的路径返回 `null`。列表类工具对每一项生效。
- 所有工具支持 `raw: true` 返回 GitHub 原始数据（体积大，仅作逃生口）；不能与 `fields` 同时使用。
- 分页工具返回 `page`、`per_page`、`count`、`has_next_page`（依据 GitHub `Link` 头）。
- `github_get_issue` 返回 `data.issue` 与 `data.comments`（`total`、`page`、`per_page`、`has_next_page`、`items`）；评论读取失败时只在 `comments.error` 中说明，不影响 issue 本身。

### 代码搜索范围与可见性

- `repo`（`owner/name`）映射为 `repo:` qualifier，优先于 `scope`；`scope` 保持原行为（组织名映射为 `org:`，`owner/repo` 映射为 `repo:`）。
- GitHub 代码搜索只索引默认分支；按版本或 tag 查 Dify 源码继续使用 dify-source。
- 结果可能包含 token 可见的私有仓库。每条结果都带 `repository.full_name` 与 `repository.private`（未知时为 `null`）。`visibility` 取 `all`（默认）、`public`、`private`；过滤时返回 `filtered_out` 和 `filter_note` 说明本页被隐藏的条数。可见性未知的结果在 `public`/`private` 过滤下都会被隐藏。

### 0 条结果说明

搜索返回 0 条且能确定目标仓库时（PR/issue 搜索的 `repo`；代码搜索的 `repo` 或 `owner/repo` 形式的 `scope`），会额外请求 `GET /repos/{owner}/{repo}`，在结果中返回 `repo_access`：

- `accessible`：`true`/`false`，探测本身失败（如网络错误）时为 `null` 并附 `error`；`http_status`；
- 可访问时返回 `private`、`visibility`、`archived`、`has_issues`、`default_branch`；issue 搜索额外给出 `issues_enabled`；
- `note` 说明 0 条的含义（如 404 表示仓库不存在或当前 token 无权访问）。

探测失败不会导致搜索本身失败。

## 开发和测试

```bash
uv sync --locked --all-extras --all-groups
uv run --locked pytest -q
cp .env.example .env
# 填写本地凭证后启动
uv run --locked github-mcp
```

使用 Python 3.12。单元测试用 `httpx.MockTransport` 模拟 GitHub API，不需要网络或真实凭据。

所有工具都在 `tools/list` 中声明 `annotations.readOnlyHint: true`、`destructiveHint: false`。OpenConnector 等客户端据此判断读写，未声明时新工具会被当作 write 并要求确认。

## 构建

```bash
docker build -t github-mcp:local .
```

Dockerfile 使用本目录完整源码和容器实际依赖版本，不依赖服务器上的旧镜像标签。生产配置、API 密钥、浏览器资料和业务数据应单独挂载，不能放入构建上下文。

## 版本来源

- 原镜像：`tool-services:latest`。
- 镜像 ID：`sha256:43da7ac7671acec3ed1c18ec8cf04a5afddf822bf77400bdb14e825c555f0453`。
- `baseline/runtime.json` 记录实际依赖与容器来源；`baseline/source-manifest.json` 记录原始文件哈希。
- `requirements.runtime.txt` 固定运行依赖；`pyproject.toml` 通过 constraints 保持同样版本。原锁文件和部署文本保存在 `baseline/`，不作为当前部署入口。
- 从该服务自己的线上 tool_services 快照拆出独立包；只调整包名、服务装配及配置字段，保留其业务实现和 MCP 传输行为。
