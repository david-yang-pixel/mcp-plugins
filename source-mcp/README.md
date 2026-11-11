# source-mcp

从腾讯云当前 `source-mcp` 运行容器整理的独立 MCP 项目。端口：`8099`。全部业务代码位于本目录 `src/source_mcp/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --all-extras --all-groups
uv run --locked pytest -q
cp .env.example .env
# 填写本地凭证后启动
uv run --locked source-mcp
```

使用 Python 3.12。需要 Git；源码仓库缓存位置由 SOURCE_MCP_REPOS_BASE_DIR 配置。

## 工具行为要点

- **ref 默认值**：`source_search`、`source_read`、`source_log`、`source_tree` 省略 `ref`（或传 `ref=runtime`）时使用 Dify SaaS 生产正在运行的代码。返回结果会带 `ref_defaulted: true` 和 `ref_warning`。Enterprise/私有部署必须显式传入客户版本对应的 tag 或锁定提交。
- **按 deployment 解析线上版本**：同一仓库的不同 deployment 可能运行不同版本（例如 `dify-api-oltp` 与 `dify-web`）。上述读取工具和 `source_diff` 都接受可选 `deployment`、`environment`，只在 ref 省略或为 `runtime` 时生效；显式 ref 时忽略并在 `deployment_warning` 中说明。未传 `deployment` 时按 `SOURCE_MCP_RUNTIME_PATH_DEPLOYMENTS`（JSON：`{"仓库": {"路径前缀": "deployment"}}`，默认 `{"dify": {"web/": "dify-web"}}`）选择：`filepath`/`path`/`glob` 落在 `web/` 下时自动改用 `dify-web`，结果带 `deployment_selection: "path"` 和 `deployment_warning`；`glob` 为空或可能跨越 `web/` 与其他目录（如全仓搜索、`**/*.tsx`）时仍用仓库默认 deployment，并在 `deployment_warning` 中提示 `web/` 下结果对应的是默认 deployment 的版本，可传 `deployment=dify-web`。结果中的 `deployment`、`environment`、`image_tag` 为实际使用值。`source_diff` 的 `runtime` 字段列出 runtime 侧的解析结果。
- **source_runtime_ref / environment**：默认按 `SOURCE_MCP_RUNTIME_ENVIRONMENT`（production）过滤 `deployment.environment`；可传 `environment`，`any` 表示不过滤。显式传 `deployment` 而默认环境查不到时，会自动不限环境重查一次，返回实际 `environment`、`environment_fallback: true` 和 `environment_warning`（用于 `dify-api-oltp-gray` 这类非 production 环境）。`SOURCE_MCP_RUNTIME_DEPLOYMENTS` 的值为逗号分隔的候选 deployment，按顺序尝试；默认已包含 `dify-sandbox`、`dify-marketplace`（线上 marketplace 各服务的镜像版本如 `2.3.2` 来自新的 `dify-marketplace` 单仓库；旧仓库 `dify-marketplace-backend` 的 tag 只到 1.0.x，不再映射线上 deployment，需显式传 ref）、`dify-billing`、`rbac`（deployment `dify-rbac`）、`knowledge-fs`（deployment `dify-knowledge-fs`，镜像 `langgenius/knowledge-fs-api`，tag 如 `1.0.5` 与 git tag 同名）。
- **线上版本的其他来源**：
  - **组件 tag 前缀**：单仓库按组件打 tag（如 `dify-marketplace` 的镜像 tag 是 `2.3.2`，git tag 是 `backend/2.3.2`、`ops/2.3.2`）。`SOURCE_MCP_RUNTIME_TAG_PREFIXES`（JSON：仓库 → {deployment 或 `*` → 前缀}）配置后优先尝试带前缀的 tag，默认已配置 `dify-marketplace` 各 deployment。未配置时，若仓库里只有一个 `*/<镜像 tag>` 形式的 tag，也会自动使用。
  - **GitOps 兜底**：SigNoz 里查不到镜像 tag 时（如 `dify-sandbox` 不上报日志、`dify-rbac` 生产没有日志），读取 `SOURCE_MCP_GITOPS_REPO`（默认 `saas-deploy`）默认分支下 `environments/<环境>/**/kustomization.yaml` 中声明的镜像 tag。deployment → 镜像名由 `SOURCE_MCP_GITOPS_IMAGES` 配置，默认含 `dify-rbac`、`dify-sandbox`、`dify-billing`、`dify-knowledge-fs`。结果带 `source: "gitops"`、`gitops`（仓库、路径、提交）和 `source_warning`，说明这是声明的期望状态而非观测值。只使用已有的本地克隆，不会在请求中克隆。
  - **副本数为 0**：GitOps 兜底会读取 overlay 的 `replicas`。该 deployment 被缩到 0 副本时返回 `RUNTIME_REF_UNAVAILABLE`，不把声明的 tag 当作线上版本。
  - **billing**：SaaS 计费运行在 `dify-saas-billing*`（镜像 `langgenius/dify-saas-billing`），`dify-gateway`、`dify-saas-admin*` 等也来自同一仓库 `langgenius/dify-saas`（依据 saas-deploy `promotions/config.json`）。该仓库没有访问权限，未配置，查询这些名称时只返回提示。旧的 `dify-billing` 在 saas-deploy 中为 0 副本，解析时返回不可用并说明已迁移。
  - **组件反查**：`SOURCE_MCP_REPO_COMPONENT_DEPLOYMENTS`（JSON：仓库 → deployment 列表）把单仓库的其他组件（如 `dify-marketplace-scheduler`）映射回仓库，只用于 `list_runtime_deployments` 的反查。`dify-knowledge-fs-*-worker` 运行的是 `langgenius/dify-api` 镜像，默认映射回 `dify`，而不是 `knowledge-fs`。
  - **dify-sandbox**：SaaS 生产的代码沙箱运行在 AWS App Runner（saas-deploy `dify-shared-sandbox-config` 的 `CODE_EXECUTION_ENDPOINT`），不在 k8s，也不向 SigNoz 上报日志，无法确定线上版本，请显式传 ref。这类说明由 `SOURCE_MCP_RUNTIME_NOTES` 配置，附在不可用错误中。
- **工具注解**：除 `refresh_repos` 外，所有工具都在 `tools/list` 中声明 `annotations.readOnlyHint: true`、`destructiveHint: false`。OpenConnector 等客户端据此判断读写；未声明时，`source_tree`、`list_runtime_deployments` 曾被判为 write 并要求确认。`refresh_repos` 会克隆、拉取、deepen/unshallow 服务器上的共享克隆缓存，占用磁盘、CPU 和网络，因此声明 `readOnlyHint: false`（不删除数据，`destructiveHint: false`），调用时需要确认；日常更新由后台定时刷新完成。
- **list_runtime_deployments**：通过同一 SigNoz `sz_run query` 通道列出最近 `hours`（默认 `SOURCE_MCP_RUNTIME_HOURS`，最大 168）内上报过 `container.image.tag` 的 `k8s.deployment.name`，每行含 `deployment`、`environment`、`image_tag`、`log_count`、`latest_ts`/`latest_at` 和映射到的 `repos`。可选 `name_contains`（仅 `[a-z0-9.-]`）、`environment`（不传则不过滤环境）、`limit`（默认 200，最大 500，超出时 `truncated: true`）。同时返回已配置的 repo→deployment 映射、路径映射、deployment→repo 反查，以及 `configured_without_logs`：只统计名称匹配 `name_contains` 的已配置 deployment；结果被 `limit` 截断时为 `null`（缺席不能说明没有日志）。
- **仓库列表**：生产 `deploy/runtime.json` 的 `SOURCE_MCP_REPOS` 共 15 个仓库。marketplace 代码在单仓库 `dify-marketplace`，旧的 `dify-marketplace-backend`、`dify-marketplace-frontend` 仓库已移除（`dify-marketplace-backend` 仍是 `dify-marketplace` 仓库的 deployment 名，出现在 `SOURCE_MCP_RUNTIME_DEPLOYMENTS` 等映射中）。
- **未配置的仓库**：`UNKNOWN_REPO` 会列出已配置仓库，并说明如何通过 `SOURCE_MCP_REPOS`（langgenius 仓库名，逗号分隔）和 `SOURCE_MCP_REPO_URLS`（非 langgenius 仓库的克隆地址）添加。`SOURCE_MCP_REPO_HINTS`（JSON：名称 → 提示文本，或 `{repo, paths, note}` 列表）为不是独立仓库的模块给出“位于 X 仓库的 Y 路径”的建议；默认包含 `billing`→`dify-billing`，`ops`/`ops-backend`/`ops-web`→`dify-marketplace` 的 `ops/`，`gateway`→`dify-enterprise` 的 `server/cmd/gateway/`、`server/pkg/gateway/`（未确认），`sandbox-gateway`→`dify-enterprise` 的 `server/cmd/sandbox-gateway/`。名称带 `dify-` 前缀时也会匹配去掉前缀的提示。
- **浅克隆与补取**：读取类工具遇到本地没有的完整 40 位提交 SHA 时，会自动从 origin 按 SHA 拉取，存放在 `refs/source-mcp/commits/<sha>`，不受 `fetch --prune` 影响。缩写 SHA 无法从远端按 ID 拉取，`REF_NOT_FOUND` 会说明仓库是浅克隆，并提示改用完整 SHA 或 `unshallow`。`repo_status`、`source_log` 返回 `shallow`；浅克隆下历史不完整时，`source_log` 会附带 `warning`。
- **完整历史**：`SOURCE_MCP_FULL_HISTORY_REPOS`（逗号分隔，`*` 表示全部，默认 `*`）中的仓库新克隆时使用完整克隆，后台定时刷新时若仍是浅克隆会执行 `fetch --unshallow`（超时 `SOURCE_MCP_GIT_HISTORY_TIMEOUT_SECONDS`）；不在列表中的仓库新克隆为 `--depth=1`，后台不会补全历史。`repo_status` 返回 `full_history`。
- **后台定时刷新**：服务启动后由 FastAPI lifespan 启动后台任务，首次在启动 60 秒（或更短的间隔）后运行，之后每 `SOURCE_MCP_BACKGROUND_REFRESH_SECONDS`（默认 3600，`<=0` 关闭）逐仓库执行：未克隆则克隆，已克隆且距上次 fetch 超过 `SOURCE_MCP_FETCH_TTL_SECONDS` 则 `fetch --all --prune --tags --force`，再按需 unshallow。单个仓库失败（如私有仓库无权限）只记录错误，不影响其他仓库。`repo_status` 返回 `last_fetch_at`/`last_fetch_at_iso`、`stale`（超过 2 倍间隔未 fetch；后台关闭时为 null）、`stale_after_seconds` 和 `background_refresh`（最近一次尝试、成功、动作、错误）。
- **分支名解析**：显式传入分支名（如 `main`、`hotfix/1.18.0-fix.4`）时，存在 `origin/<分支>` 就用远端跟踪分支，不用本地 `refs/heads/*`（本地分支只在克隆或旧的按需拉取时写入，之后不会更新）；同名 tag 仍优先。只有本地分支、还没有 `origin/<分支>` 时，会先按需拉取远端分支；拉取失败（无法访问 origin、上游已删除）才退回本地分支。按需拉取的分支写入 `refs/remotes/origin/*`，`refresh_repos` 的 `refs` 参数也按同样规则解析。读取、日志、diff 都使用解析出的提交。
- **拉取范围**：默认只拉取远端默认分支（`+refs/heads/<默认分支>:refs/remotes/origin/<默认分支>`）和全部 tag，不拉取全部分支。默认分支名从 `git ls-remote --symref origin HEAD` 读取一次，缓存在克隆的 git 配置 `source-mcp.defaultbranch` 中，并同步 `origin/HEAD`。其他分支（如线上版本对应的 `hotfix/*`）在用到时按需拉取单个分支；之后再次使用且距上次拉取超过 `SOURCE_MCP_FETCH_TTL_SECONDS` 时，重新拉取这一个分支。`SOURCE_MCP_FETCH_ALL_BRANCHES=true` 可改为拉取全部分支。
- **refspec 修复**：`git clone --depth=1` 或 `--single-branch` 会把 refspec 固定为克隆时的那个分支。如果克隆时在 hotfix 分支上，之后 `fetch --all` 虽然成功，`origin/main` 却不再更新（dify 曾因此落后数千个提交）。每次 fetch 前都会检查 `remote.origin.fetch`，与上面的期望值不一致时自动修正（后台刷新记为 `refspec-repaired`）。
- **repo_status 摘要与 detail**：默认（`detail` 省略或为 `false`）每个仓库只返回 `repo`、`commit`、`default_branch_committed_at`、`stale`、`shallow`、`last_error` 六个字段；`last_error` 取后台刷新的 `background_refresh.last_error`，仓库本身无法检查时为 `<code>: <error>`；未克隆的仓库其余字段为 `null`。传 `detail: true` 返回下述完整字段（含 `path`、`cloned`、`full_history`、`last_fetch_at`/`last_fetch_at_iso`、`background_refresh` 等）。指定 `repo` 时同样适用。
- **repo_status 字段（detail）**：`commit` 和 `default_branch_committed_at` 是默认分支（`origin/HEAD`，通常为 `origin/main`）的最新提交及其时间；检出的 HEAD 单独报告为 `head_ref`、`head_commit`。另返回 `fetch_refspecs`、`fetch_refspec_ok`。`last_fetch_at` 是最近一次全量 `fetch --all` 的时间（`last_fetch_kind: "full"`）；按需拉取单个 ref 也会改写 `FETCH_HEAD`，不再据此判断新鲜度。
- **refresh_repos / repo_status**：`refresh_repos` 返回 `changed`、`changes`（新增、更新、删除的 ref 数及 `history_changed`）、`shallow` 和 `warnings`，可以分辨"成功但没变化"。多仓库时单个仓库失败不会让整个调用失败：该条目为 `ok: false`，附 `code` 和 `error`，成功条目为 `ok: true`；只指定一个仓库时失败仍直接返回错误。只指定一个仓库时可传：`refs`（分支、tag 或完整 SHA，最多 20 个）、`deepen=N`、`unshallow=true`。历史拉取使用 `SOURCE_MCP_GIT_HISTORY_TIMEOUT_SECONDS`（默认 900 秒）。不带 `repo` 的 `repo_status` 同样逐仓库隔离错误。
- **调用审计日志**：每次 `tools/call` 向 stdout 输出一行 JSON（logger `source_mcp.audit`，INFO，格式同 difyctl-mcp），字段为 `time`（UTC `%Y-%m-%dT%H:%M:%SZ`）、`tool`、`duration_ms`、`outcome`（`success`/`failed`）、`error`（错误码或 `null`）、`response_chars`（返回文本长度）和 `args`。`args.keys` 只列参数名；`args.values` 只记录 `repo`、`repos`、`deployment`、`environment`、`ref`/`ref1`/`ref2`/`refs` 及 `mode`、`limit`、`detail` 等标量选项，搜索 `pattern`、`glob`、`filepath`、`path` 等其他参数只记名称不记值。`create_app()`/`main()` 为该 logger 单独挂 stdout handler（不配置 root logger，不改变其他日志）；写日志失败不会影响工具调用。`tools/list` 等非工具调用不记录。
- **source_search**：`mode=auto`（默认）按扩展正则匹配，`a|b` 可用；遇到 lookaround、`\d`、`(?` 分组或懒惰量词时改用 PCRE；开头的 `(?i)` 会被识别。正则被拒绝时退回固定字符串，并在 `fallback` 中说明。也可以显式指定 `fixed`/`regex`/`perl`，此时正则非法返回 `BAD_PATTERN`。另有 `case_sensitive`（默认 false）和 `context`（0–10 行上下文）。`limit` 是最多返回的匹配行数（默认 30，上限 200），`offset` 跳过前若干条；返回结构化 `matches`（`file`、`line`、`text`，有 context 时附 `context_before`/`context_after`，单行超过 500 字符截断并标 `text_truncated`）、`total_matches`、`total_exact`（git grep 输出超过 4MB 时为 false，总数为下限）、`files_with_matches`、`returned`、`offset`、`has_more`、`next_offset`。`output` 文本只包含当前页，与分页一致。跳过二进制文件。无匹配时的输出写明实际使用的模式和大小写设置。
- **source_tree**：基于 `git ls-tree` 列出目录，`path` 省略为仓库根目录，`recursive=true` 列出整个子树（含子目录条目）。条目含 `path`、`type`（`blob`/`tree`/`commit`）、`mode`，文件含 `size`；`path` 指向文件时返回该文件一条。`limit` 默认 200、上限 1000，配合 `offset`/`next_offset` 分页。拒绝 `..` 和绝对路径（`BAD_FILEPATH`），路径不存在返回 `PATH_NOT_FOUND`。
- **source_read**：`start_line`/`end_line` 为从 1 开始、包含两端的行号。返回 `total_lines`、`start_line`、`end_line`、`truncated`；单次最多 120000 字符，按整行截断，并提示下一次的 `start_line`。
- **source_diff**：`mode=stat`（不传 `filepath` 时的默认值）、`name-status`、`patch`（传 `filepath` 时的默认值）。`filepath` 可以是目录。
- **list_repo_tags**：可传 `pattern`（`git tag -l` 的 glob，如 `1.14.*`）。能解析为版本号的 tag 按版本降序排在前面，允许 `v` 前缀和组件前缀（如 `1.14.2`、`v1.14.2-fix.4`、`dify-3.13.2`、`backend/2.3.2`、`redhat-chart-3.9.8-run-1`）；同一版本纯版本号在前、带前缀的在后；`-rc`/`-beta` 等预发布排在正式版之后，`-fix.N` 等后缀排在正式版之前；其余 tag（如 `main`、`latest`）按名称排在后面。返回 `total`、`version_tags`、`non_version_tags`。

## 构建

```bash
docker build -t source-mcp:local .
```

Dockerfile 使用本目录完整源码和容器实际依赖版本，不依赖服务器上的旧镜像标签。生产配置、API 密钥、浏览器资料和业务数据应单独挂载，不能放入构建上下文。

## 版本来源

- 原镜像：`source-mcp:3741-20260803.2`。
- 镜像 ID：`sha256:3a7d3fbf2845e8bd75c94d5221bdc154d07b24f90015dd7ab23422bb2a55b29a`。
- `baseline/runtime.json` 记录实际依赖与容器来源；`baseline/source-manifest.json` 记录原始文件哈希。
- `requirements.runtime.txt` 固定运行依赖；`pyproject.toml` 通过 constraints 保持同样版本。原锁文件和部署文本保存在 `baseline/`，不作为当前部署入口。
- 原功能说明见 [baseline/README.original.md](baseline/README.original.md)。

项目声明补齐线上代码实际导入的 `httpx` 运行依赖；版本保持与容器一致。
