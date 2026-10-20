# source-mcp

从腾讯云当前 `source-mcp` 运行容器整理的独立 MCP 项目。端口：`8099`。全部业务代码位于本目录 `src/source_mcp/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest
cp .env.example .env
# 填写本地凭证后启动
uv run --locked source-mcp
```

使用 Python 3.12。需要 Git；源码仓库缓存位置由 SOURCE_MCP_REPOS_BASE_DIR 配置。

## 工具行为要点

- **ref 默认值**：`source_search`、`source_read`、`source_log` 省略 `ref` 时使用 `runtime`，即 Dify SaaS 生产正在运行的代码。返回结果会带 `ref_defaulted: true` 和 `ref_warning`。Enterprise/私有部署必须显式传入客户版本对应的 tag 或锁定提交。
- **浅克隆与补取**：读取类工具遇到本地没有的完整 40 位提交 SHA 时，会自动从 origin 按 SHA 拉取，存放在 `refs/source-mcp/commits/<sha>`，不受 `fetch --prune` 影响。缩写 SHA 无法从远端按 ID 拉取，`REF_NOT_FOUND` 会说明仓库是浅克隆，并提示改用完整 SHA 或 `unshallow`。`repo_status`、`source_log` 返回 `shallow`；浅克隆下历史不完整时，`source_log` 会附带 `warning`。
- **refresh_repos**：返回 `changed`、`changes`（新增、更新、删除的 ref 数及 `history_changed`）、`shallow` 和 `warnings`，可以分辨"成功但没变化"。只指定一个仓库时可传：`refs`（分支、tag 或完整 SHA，最多 20 个）、`deepen=N`、`unshallow=true`。历史拉取使用 `SOURCE_MCP_GIT_HISTORY_TIMEOUT_SECONDS`（默认 900 秒）。
- **source_search**：`mode=auto`（默认）按扩展正则匹配，`a|b` 可用；遇到 lookaround、`\d`、`(?` 分组或懒惰量词时改用 PCRE；开头的 `(?i)` 会被识别。正则被拒绝时退回固定字符串，并在 `fallback` 中说明。也可以显式指定 `fixed`/`regex`/`perl`，此时正则非法返回 `BAD_PATTERN`。另有 `case_sensitive`（默认 false）和 `context`（0–10 行上下文）。无匹配时的输出写明实际使用的模式和大小写设置。
- **source_read**：`start_line`/`end_line` 为从 1 开始、包含两端的行号。返回 `total_lines`、`start_line`、`end_line`、`truncated`；单次最多 120000 字符，按整行截断，并提示下一次的 `start_line`。
- **source_diff**：`mode=stat`（不传 `filepath` 时的默认值）、`name-status`、`patch`（传 `filepath` 时的默认值）。`filepath` 可以是目录。

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
