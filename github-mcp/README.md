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
| `github_get_file` | 读取单个文件（解码文本）或列出目录 | `repo`、`path`、`ref`、`max_chars`（默认 20000） |

### 写工具（默认关闭）

只有用写密钥 `GITHUB_MCP_WRITE_API_KEY` 连接、且 `write_active` 为真时，以下工具才会出现在 `tools/list` 中并可调用。详见下文“写操作与安全”。

| 工具 | 用途 | 主要参数 |
| --- | --- | --- |
| `github_fork_repo` | fork 仓库到自己账号或允许的组织（异步，返回 `ready: false`） | `repo`、`organization`、`name`、`default_branch_only` |
| `github_create_repo` | 新建仓库 | `name`、`org`、`description`、`private`（默认 false）、`auto_init` |
| `github_create_branch` | 从分支、tag 或 SHA 建分支（默认默认分支） | `repo`、`branch`、`from_ref` |
| `github_commit_files` | 一次原子提交多个文件的新增、修改、删除；从不强推 | `repo`、`branch`、`message`、`files[{path, content, encoding, executable}]`、`delete_paths`、`expected_head_sha` |
| `github_create_pr` | 开 PR；`head` 可写 `owner:branch` 从 fork 提交 | `repo`、`title`、`head`、`base`、`body`、`draft`、`maintainer_can_modify` |
| `github_create_issue` | 开 issue | `repo`、`title`、`body`、`labels`、`assignees` |
| `github_add_comment` | 在 issue 或 PR 会话下评论 | `repo`、`issue_number`、`body` |

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

### 仓库不可访问

限定仓库的搜索（PR/issue 搜索，或代码搜索带 `repo`/`owner/repo` 形式的 `scope`）在目标仓库不存在或 token 无权访问时，GitHub 返回 HTTP 422 而不是 0 条结果。此时服务同样探测 `GET /repos/{owner}/{repo}`：确认不可访问（`accessible: false`）时返回 `success: false`、`code: "REPO_NOT_ACCESSIBLE"`，并附带上述 `repo_access`；仓库可访问（如查询语法错误）或探测结果不确定时，保留原始 `GITHUB_API_ERROR` 错误。

## 写操作与安全

写操作在发出任何 GitHub 请求前依次经过以下检查：

1. **写密钥**：用 `GITHUB_MCP_API_KEY` 连接时只能看到和调用只读工具，调用写工具返回 `FORBIDDEN_SCOPE`；用 `GITHUB_MCP_WRITE_API_KEY` 连接才看得到写工具。写密钥为空或与只读密钥相同时，永远不会授予写权限。
2. **总开关**：`GITHUB_MCP_WRITE_ENABLED=true`，且写密钥、写 token 都已配置，否则写工具不出现，调用返回 `WRITE_DISABLED`。
3. **仓库白名单**：
   - `GITHUB_MCP_WRITE_OWNERS`（逗号分隔，默认 `david-yang-pixel`）：只有这些账号或组织下才能建仓库、建分支、提交；fork 的目标账号也必须在这里。代码不会直接推到别人的仓库。
   - `GITHUB_MCP_UPSTREAM_REPOS`（逗号分隔，支持 `owner/*`）：除自己名下的仓库外，只有这些仓库允许开 issue、开 PR、发评论。
   - `GITHUB_MCP_PR_FROM_OWN_FORK`（布尔，默认 `false`）：设为 `true` 后，`github_create_pr` 的目标仓库可以是任意仓库，前提是 `head` 写成 `owner:branch` 且该 `owner` 在 `GITHUB_MCP_WRITE_OWNERS` 里（即 PR 的来源分支在自己账号的 fork 中）。`head` 不带 `owner:` 前缀时仍按上面两条判断。只放开开 PR；开 issue、发评论以及建分支、提交、fork 的规则不变。
   - 不在名单内返回 `REPO_NOT_ALLOWED`。fork 未指定组织时会先请求 `GET /user` 确认写 token 的账号，再做白名单检查。
4. **频率限制**：每个进程每小时最多 `GITHUB_MCP_WRITE_RATE_PER_HOUR` 次写操作（默认 30），超出返回 `RATE_LIMITED`。

写请求只使用 `GITHUB_MCP_GITHUB_WRITE_TOKEN`，不会回退到只读 token；只读工具也不会使用写 token。没有删除仓库、删除分支或强推的工具，`github_commit_files` 固定 `force: false`，分支移动时返回 `HEAD_MOVED` 且不覆盖任何内容。

写 token 建议使用只勾选 `public_repo` 的经典 PAT（classic）：只有经典 token 能写不属于自己的公开仓库（给上游提 issue、PR、评论）。只在确实需要写私有仓库时才用 `repo`。

其他错误码：`REPO_EXISTS`、`BRANCH_EXISTS`、`BRANCH_NOT_FOUND`、`REF_NOT_FOUND`、`EMPTY_REPOSITORY`、`PR_INVALID`。

## 审计日志

每次 `tools/call` 向 stdout 输出一行 JSON（logger `github_mcp.audit`，入口 `main_github()` 将 INFO 日志配置到 stdout）：

```json
{"time": "2026-11-11T08:41:43Z", "tool": "github_search_issues", "duration_ms": 412, "outcome": "failed", "error": "REPO_NOT_ACCESSIBLE", "response_chars": 520, "args": {"keys": ["query", "repo"], "values": {"repo": "langgenius/dify-saas"}}}
```

- `outcome` 为 `success`/`failed`，`error` 为错误码（成功时为 `null`），`response_chars` 为返回文本长度。
- `args.keys` 只记录参数名；`args.values` 只记录非敏感参数（如 `repo`、`scope`、`pr_number`、`issue_number`、`page`、`limit`）。搜索词 `query`、`path`、`fields` 等只记录键名，不记录值；返回内容不写入日志。
- 写工具的日志额外带 `write: true`、`scope` 和 `result`（只含 `html_url`、`sha`、`number`、`full_name`、`id`）；`files`、`delete_paths`、`labels`、`assignees` 只记录数量（`*_count`）。`title`、`body`、`message`、文件路径与内容不写入日志。
- 写日志失败不会影响工具调用。

## 开发和测试

```bash
uv sync --locked --all-extras --all-groups
uv run --locked pytest -q
cp .env.example .env
# 填写本地凭证后启动
uv run --locked github-mcp
```

使用 Python 3.12。单元测试用 `httpx.MockTransport` 模拟 GitHub API，不需要网络或真实凭据。

只读工具在 `tools/list` 中声明 `annotations.readOnlyHint: true`、`destructiveHint: false`；写工具声明 `readOnlyHint: false`，`github_commit_files` 另声明 `destructiveHint: true`。OpenConnector 等客户端据此判断读写，写工具需要显式确认后才执行。

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
