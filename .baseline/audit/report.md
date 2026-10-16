# MCP 仓库基线核对

核对日期：2026-10-16（Asia/Singapore）。目标主机：`zendesk-agent-server`。范围：8 个服务；不包含 `sandbox-worker`、OpenConnector 或其他 Agent 容器。

结论：首版仓库以各服务当前容器文件快照为代码基线。服务器目录只有在 SHA-256 完全匹配时才作为取材来源。本地目录不适合整体作为首版基线。8 个服务的来源已逐项确定；本次没有改动服务代码、创建仓库或部署。

## 方法与验证范围

- 通过对应容器解释器的模块查找确认 8 个包均从 `/app/src` 解析；未导入业务应用或调用业务工具。
- 对源码、测试、脚本、README、pyproject.toml、uv.lock 及选定部署文本逐文件计算 SHA-256。排除 `.env`、生产配置、凭证、浏览器资料、业务数据、缓存、虚拟环境和构建产物。
- 收集了 442 份文件记录（包含不同位置的相同文件）；286 份 Python 文件通过 Python 3.12 AST 语法解析。
- 核对结束时再次读取容器 ID、镜像 ID 和所有已采集远程文件哈希，未发现核对期间变化。
- 此次验证的是文件基线和依赖元数据，未运行全量测试、重建镜像、重启服务或验证客户业务端到端路径。语法通过不等于功能验收。

## 三方源码比较

下表只统计 `src/` 文件；“改 N / 多 N”以线上容器为参照。GitHub、文档、通知的本地公共包指 `tool-services`，独立本地目录另列说明。

| 服务 | 线上 vs 服务器常规目录 | 线上 vs 本地对应包 | 基线取材 |
| --- | --- | --- | --- |
| `db-gateway` | 一致（14 文件） | 改 1 | 容器完整项目 + 已匹配的服务器 src |
| `signoz-mcp` | 一致（5 文件） | 改 1 | 服务器 code |
| `source-mcp` | 改 3；线上多 1 | 改 3；线上多 1 | 已匹配的构建目录 |
| `github-mcp` | 改 1 | 改 2 | GitHub 运行容器快照 |
| `docs-tool-service` | 一致（8 文件） | 改 1 | 服务器 tool-services/code |
| `notification-service` | 改 3 | 改 2 | 通知运行容器快照 |
| `zendesk-mcp` | 一致（6 文件） | 改 2 | 服务器 code + 测试 |
| `zendesk-browser-relay` | 一致（15 文件） | 改 7；线上多 4 | 服务器项目目录 |

## 每个服务的取材决定

### db-gateway

以运行容器的源码、pyproject.toml、uv.lock 和脚本为基线；服务器 /opt/dify-db/src 已逐文件匹配。服务器根目录缺少项目元数据，不能只复制该目录。

本地 app.py 缺少线上 GET /sse 别名、SSE 持续连接及 15 秒心跳；本地 uv.lock 也不同。容器锁文件中可对照的已安装包版本一致。

- 常规服务器目录：`/opt/dify-db`
- 当前镜像：`db-gateway:latest`
- 镜像 ID：`sha256:378972148a4081b135ca2fdf3870bd36dbc04940fc38a8c185048efd4465887e`
- 源码树指纹：`043d9cb0c5576063b1483d9c784b8d4d007c6156902af4b85d45d5c20f55a640`
- 与本地不同：`src/db_gateway/app.py`

### signoz-mcp

采用 /opt/signoz-mcp/code；本次收集范围内所有文件与容器一致。记录容器 app.py 可写层修改和实际依赖。

本地 app.py 缺少线上 GET /mcp SSE 入口、服务前缀 endpoint 和持续心跳。其余 4 个源码文件一致。

- 常规服务器目录：`/opt/signoz-mcp/code`
- 当前镜像：`signoz-mcp:latest`
- 镜像 ID：`sha256:85de4f2b94af5defd28ea15363566b66fec3bf8cc550dd9c43388c642980f2a0`
- 源码树指纹：`2cd3ff9327b6b256d6aa7ae3ecac6fd81087a879ce835a707c3735c2d9227c69`
- 与本地不同：`src/signoz_mcp/app.py`

### source-mcp

采用 /opt/dify-db/build/source-mcp-3741-concurrency2-20260803；源码、测试、README、锁文件等本次收集文件均与运行容器完全一致。不要以 /opt/source-mcp/code 为基线。

线上多出 process_health.py；app.py、git_ops.py、settings.py 与常规服务器目录和本地存在差异。线上包括进程健康统计、全仓搜索并发限制、Git 超时/取消后的进程组回收、禁用搜索时的隐式 lazy fetch，以及缺少 blob 的独立错误。匹配的构建目录还含新增测试。

- 常规服务器目录：`/opt/source-mcp/code`
- 当前镜像：`source-mcp:3741-20260803.2`
- 镜像 ID：`sha256:3a7d3fbf2845e8bd75c94d5221bdc154d07b24f90015dd7ab23422bb2a55b29a`
- 源码树指纹：`8996d601da22ac4a8f9a2778950c50593491bdca52ed8a5f74da85c164b97a39`
- 与本地不同：`src/source_mcp/app.py`, `src/source_mcp/git_ops.py`, `src/source_mcp/settings.py`
- 本地缺少：`src/source_mcp/process_health.py`

### github-mcp

保留当前 GitHub 容器的完整 tool_services 包版本。服务器副本只有 docs_tools.py 不同，GitHub 工具、app.py、mcp.py、settings.py 均已匹配，但整包不是同一版本。

与服务器整包相比，仅 docs_tools.py 不同；这不表示 GitHub 工具本身落后。与本地 tool-services 相比还差 app.py 的 SSE 改动。本地独立 github-mcp 的 tools.py 和 mcp.py 与线上对应模块完全一致，但应用装配、settings 和包结构不一致。

- 常规服务器目录：`/opt/tool-services/code`
- 当前镜像：`tool-services:latest`
- 镜像 ID：`sha256:43da7ac7671acec3ed1c18ec8cf04a5afddf822bf77400bdb14e825c555f0453`
- 源码树指纹：`e2318cddf39265afbfe99e4c57c60818a601de8f999c449bf1009e8ce89684b7`
- 与本地不同：`src/tool_services/app.py`, `src/tool_services/docs_tools.py`

### docs-tool-service

采用 /opt/tool-services/code，当前文档容器与其收集文件完全一致。保留完整文档检索和官网读取能力。

本地 tool-services 的 docs_tools.py 已与线上一致，差异是 app.py 的 SSE 处理；但本地独立 docs-tool-service/tools.py 明显较旧，缺少当前文档全文语料检索、分页读取，以及 official_site_list/search/read 等官网工具。不得选错本地目录。

- 常规服务器目录：`/opt/tool-services/code`
- 当前镜像：`tool-services:latest`
- 镜像 ID：`sha256:293a6a806085797799cafdaa269ed0cce2b03a37471fe10ce9556dfee95d4f21`
- 源码树指纹：`d3b247492c1f55e8f0348f6151af6c0a5ef3c8bfd827b30afa170a59a6faba5d`
- 与本地不同：`src/tool_services/app.py`

### notification-service

保留当前通知容器的完整 tool_services 包版本。notification_tools.py 与服务器及本地独立通知工具一致；服务器 app.py 已使用不同的 MCP/SSE 处理，不能静默合并为线上同版本。

通知核心 notification_tools.py 及通用 mcp.py、settings.py 与服务器一致；整包差异为 app.py、docs_tools.py、github_tools.py。通知容器的 app.py 仍与本地 tool-services 一致。把服务器公共包直接替换过去会改变通知服务的 MCP/SSE 行为。

- 常规服务器目录：`/opt/tool-services/code`
- 当前镜像：`tool-services:latest`
- 镜像 ID：`sha256:fd4449475f9d4f5eba5a8932fbfe172d82c88d42446672210df705531d92e058`
- 源码树指纹：`c9b3efe4aeb0c6d9096ba6d34ef042531f37b5a9223b408d9ed7a0673f5f0e53`
- 与本地不同：`src/tool_services/docs_tools.py`, `src/tool_services/github_tools.py`

### zendesk-mcp

采用 /opt/zendesk-mcp/code；源码及容器内已有元数据一致，并保留服务器的附件 Accept 回归测试。

本地缺少 app.py 的持续 SSE 改动，以及 client.py 下载附件时的 Accept: */* 设置。服务器还有本地没有的 tests/test_attachment_accept.py。

- 常规服务器目录：`/opt/zendesk-mcp/code`
- 当前镜像：`zendesk-mcp:accept-fix-20261012`
- 镜像 ID：`sha256:08cf136ea5767039e6b7b7bcf3712793295f126b37817d8a2d28007088e682f3`
- 源码树指纹：`b4e7ccff24d927bfa8c8982b6985ca95b0bc75a6c61f1c2370703e22a84cd854`
- 与本地不同：`src/zendesk_mcp/app.py`, `src/zendesk_mcp/client.py`

### zendesk-browser-relay

采用 /opt/zendesk-browser-relay；15 个源码文件、pyproject.toml、README 与容器一致，保留服务器测试和脚本。uv.lock 与实际已安装依赖仍有差异，须另外记录。

本地 7 个源码文件不同，并缺少 feishu_notifications.py、result_dispatch.py、session_supervisor.py、zendesk_freshness.py。线上还包含结果回调/继续运行、人工交接状态、发送前消息新鲜度核对、会话维护和飞书状态通知。线上 pyproject 要求 Python >=3.12，本地仍是 >=3.9。

- 常规服务器目录：`/opt/zendesk-browser-relay`
- 当前镜像：`zendesk-browser-relay:multilingual-footer-20261003`
- 镜像 ID：`sha256:069bd86ab5f622691eb8ff36f2a911bcb6f73830056ffb9c8bbdf2a1ced9e991`
- 源码树指纹：`abf3bc6cfea3873e22a675dacabee79e272d6fa660ce8149e7b4a693606e6c8a`
- 与本地不同：`src/zendesk_browser_relay/app.py`, `src/zendesk_browser_relay/auth.py`, `src/zendesk_browser_relay/browser_relay.py`, `src/zendesk_browser_relay/coordination.py`, `src/zendesk_browser_relay/mcp.py`, `src/zendesk_browser_relay/schemas.py`, `src/zendesk_browser_relay/settings.py`
- 本地缺少：`src/zendesk_browser_relay/feishu_notifications.py`, `src/zendesk_browser_relay/result_dispatch.py`, `src/zendesk_browser_relay/session_supervisor.py`, `src/zendesk_browser_relay/zendesk_freshness.py`

## 不能忽略的版本差异

1. `db-gateway`、`signoz-mcp`、`github-mcp` 的 `app.py` 被 Docker 标记为可写层修改。对应文件 mtime 均早于该容器本次启动。基线必须保留实际文件及哈希，不能只记录镜像标签或 ID。Docker diff 的目录项变化没有计作源码文件变化。
2. GitHub、文档、通知三个容器都显示 `tool-services:latest`，但镜像 ID、源码快照和实际依赖均不同。首版分别保留部署记录；不把统一公共包当作已经部署的相同版本。
3. 三个服务当前都导入完整 `tool_services` 包，其中 app.py 还引用 sandbox_tools。排除 sandbox-worker 的部署与维护目标，不等于可以在未修改装配代码时删除这个共享模块。拆分公共包应作为后续有测试的改动。
4. 多数 uv.lock 未反映当前已安装依赖。代码基线已确定，能重建同样运行环境的依赖基线仍需据实整理和验证；此次没有改锁文件。

| 服务 | 对照锁文件 | 已安装包与锁中版本不一致的数量 |
| --- | --- | --- |
| `db-gateway` | 容器 uv.lock | 0 |
| `signoz-mcp` | 容器 uv.lock | 8 |
| `source-mcp` | 容器 uv.lock | 8 |
| `github-mcp` | 容器 uv.lock | 8 |
| `docs-tool-service` | 容器 uv.lock | 16 |
| `notification-service` | 容器 uv.lock | 8 |
| `zendesk-mcp` | 容器 uv.lock | 16 |
| `zendesk-browser-relay` | 服务器 uv.lock（容器未包含） | 11 |

这里仅比较锁文件与实际安装集合的交集，不能把数量为 0 当作完整可重建证明。完整包版本与逐项差异见 JSON 证据。

## 补充材料与下一阶段边界

- DB Gateway 容器不含测试，服务器根目录也没有完整 pyproject/锁文件；以容器项目为主，保留本地测试和部署模板为待核验的补充材料。服务器单独的 scripts/tsh-init.py 也单列保留。
- Zendesk MCP 的服务器测试集比镜像内更完整；Browser Relay 的测试、脚本和锁文件来自服务器目录。它们没有经过本次功能测试。
- 本地独有的 Browser Relay Dockerfile.footer-hotfix、deploy_footer_hotfix.py、verify_customer_reply.py 作为历史辅助材料保留，不覆盖线上源码或直接当作部署入口。
- 下阶段若导入仓库，按本报告来源复制代码、补充测试/构建材料并保留 8 份运行清单。GitHub/通知版本合并、依赖锁定和共享包重构需独立验证；不会随初始归档隐含执行。

## 证据文件

- `baseline-selections.json`：8 个确定的源码来源、逐文件哈希、源码树指纹和运行元数据。
- `manifest.json`：线上、服务器、本地的文件哈希及实际已安装包版本，不含文件正文或凭证。
- `comparison.json`：三方逐文件新增、缺失、修改清单。
- `dependency-comparison.json`：锁文件与安装版本的差异。
- `syntax-check.json`：语法检查结果。

源码树指纹算法：按 src/ 相对路径排序，依次对 UTF-8 路径、NUL、文件 SHA-256 十六进制文本、换行进行 SHA-256 累积。
