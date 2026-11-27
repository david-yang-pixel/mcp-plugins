# dify-db：通过 Teleport 只读查询插件 Lambda 日志和集群信息 设计

日期：2026-11-27（同日按 Task 0 实测修订，见 §9）
范围：`db-gateway`（OpenConnector 连接名 `dify-db`）

## 1. 目的

db-gateway 已经持有一个自动续期的 Teleport 会话，现在只用它开数据库隧道。同一个会话还能做两件对工单排查有用的事：

1. 查 SaaS 插件 Lambda 的 CloudWatch 日志（朱浩《Teleport 查询 SaaS 插件 Lambda 指南》，飞书 wiki `UyMZw4slwiKAxWke43Bc8gKfnAh`）。
2. 只读查看生产和 staging 的 Kubernetes 集群（Pod 状态、部署的镜像版本、Pod 日志）。

本设计在 db-gateway 里加一个很薄的「tsh 只读执行层」，在它上面暴露两个新工具。不加新容器、不加新密钥、不改 watchdog 和隧道容器。

用户已决定：
- 加到 dify-db（db-gateway 服务），不新建 MCP。
- 共用 dify-db 现有 API key 和 OpenConnector `dify-db` 连接。
- 要能完整地查插件 Lambda 日志，网关不要加太多限制。
- 方案要改动小。
- 这个 MCP 只有用户本人使用，不需要防止调用方读出网关容器里的文件或环境变量。

## 2. 目标、非目标、反目标

目标
- 新增两个只读工具：`plugin_lambda_logs`、`kube_read`。
- 日志能查全：不设时间跨度上限、不截断日志内容，任何范围都能靠翻页取完。
- 证书续期后两个工具自动恢复，不需要人工登录。
- 单次工具调用总耗时小于 60 秒（Nginx 限制），由一个统一的截止时间保证。
- 改动只落在 `db-gateway/` 目录：执行层和两个工具模块、工具 schema 和分发、镜像加 aws CLI 和 tsh 18.11。不改连接池代码。

非目标
- 实时跟踪（`--follow`、`--watch`）。MCP 是请求-响应。
- 任何写操作（exec、delete、patch、apply 等）。
- 修改 `tsh-auth-watchdog`、`tsh-proxy-*`（AGENTS.md 规定不在本仓库部署范围），也不升级宿主机的 `/usr/local/bin/tsh`。
- 单独的访问控制；接入 Teleport 里能看到的其他数据库（KnowledgeFS 生产只读库、Redshift `saas-02` 等）。
- 单独的 `plugin_lambda_resolve` 工具和 HTTP 端点（见 §4）。
- 防止调用方借 kubectl/aws 参数读出容器内文件或环境变量（单人使用，用户确认不需要）。

反目标
- 静默丢日志：截断内容、超出部分不给翻页令牌、范围大就直接超时而什么都拿不到。
- 登录 app 或 kube 时打断数据库连接池，或改写线上 `.tsh` 里的任何文件。
- 证书续期后新工具静默失效。
- `kube_read` 能被用来做写操作，或换一个身份/集群访问。
- kubectl 报错却被当成成功返回。

## 3. 现状与实测

**Teleport 会话**（2026-11-27 实测，首尔服务器，`TELEPORT_HOME=/opt/dify-db/.tsh`）
- 账号 `yangyonggang`，角色 `production-read-all, staging-read-all, support-lambda-logs`；宿主机 tsh 客户端 v18.0.0，proxy 18.11.0。
- db-gateway 容器：host 网络、root、挂载 `/usr/local/bin/tsh` 和 `/opt/dify-db/.tsh → /opt/zd-agent/.tsh`。`tsh-auth-watchdog`、`tsh-proxy-db*` 也用这个 tsh 和这个目录。
- watchdog 续期时删除 `keys/`、`current-profile`、`teleport.dify.dev.yaml` 后重新 `tsh login`（约 10 秒）。
- `pg_pool.keys_signature` 监视整个 `keys/` 的文件数和最新 mtime，任何变化都会关闭三个库的连接池。
- `.tsh/bin/` 下只有 tsh 客户端自动更新的 `.config.json` 和 `.lock`，没有二进制。

**Task 0 实测结论**（详见 §9）
- **宿主机 tsh 18.0.0 不能用于 `tsh aws`**：tsh 本地代理报 `AWS signature verification failed`，aws 收到空 body 的 403。awscli 1.36–1.46 和 v2.42 都一样。换成与 proxy 同版本的 tsh 18.11.0 后正常。
- **tsh 会重写整个 key ring**：`tsh apps login`、`tsh aws`、`tsh kubectl` 每次都重写 `keys/<proxy>/` 下的用户证书、私钥、CA 和 SSH 证书。内容基本不变，但 mtime 全变，SSH 私钥文件因重新序列化内容也变。只跳过 `-app`/`-kube` 目录挡不住连接池被清空。
- **`tsh kubectl` 的退出码永远是 0**（18.0.0 和 18.11.0 都是）：Forbidden、NotFound、未知参数、context 不存在，都只体现在 stderr。
- tsh 18.11 的 `tsh aws --app` 在没有 app 证书时会自动登录 app（多 2–3 秒），不必先 `tsh apps login`。
- kubeconfig 的 exec 配置里写死了登录时的 `TELEPORT_HOME`；kube 证书被删后 `tsh kubectl` 会自动重新签发（约 3 秒）。
- 单值参数必须写成 `--filter-pattern=<值>`：以 `-` 开头的过滤式用空格写法时 aws 报 `expected one argument`。

**Lambda 日志**
- 角色 `support-lambda-logs`，app `aws-support-logs`，AWS 角色 `SupportLambdaLogsReadOnly`（账号 523286616386）。
- 镜像里没有 aws CLI，`tsh aws` 需要它。awscli 1.46.3（pip，内置 botocore 1.43.62）已验证可用。
- 插件库：`serverless_runtimes` 18,115 行，全部是 Lambda us-east-1；`function_name` 为 32 位十六进制，44 行带 `--N` 后缀；`plugin_installations` 有 `(tenant_id, plugin_id)` 唯一索引，所以「工作空间 + 插件」最多对应一条安装记录。

**Kubernetes**
- 可见两个集群：
  - `lg-eks-01`（生产，AWS 账号 523286616386）：`dify-plugin` 命名空间 262 个 Pod，是生产 SaaS 主体（`dify-api-oltp`、各 worker、`dify-gateway`、knowledge-fs、marketplace 等）；另有 `tokener`、`argocd`、`kube-system` 等。
  - `dify-plugin-1`（账号 014498624057）：`dify-staging` 95 个 Pod，另有 `dify-marketplace` 等。
- 当前角色能读：`pods`、`pods/log`、`deployments`。
- 被 Teleport 拒绝：`events`、`configmaps`、`secrets`、`nodes`、`services`、`statefulsets`、`replicasets`、`daemonsets`、`ingresses`、`hpa`、`jobs`、`pods/exec`、删除和修改。`top` 不可用（没有 Metrics API）。
- `tsh kubectl` 内置 kubectl，不需要另装；一次调用约 1–3.5 秒。
- `tsh kube login --all` 生成两个 context：`teleport.dify.dev-lg-eks-01-us-east-1-523286616386`、`teleport.dify.dev-dify-plugin-1-eks-us-east-1-014498624057`。
- `--context` 放在 kubectl 子命令之后。18.0.0 放在前面会报错，18.11.0 前后都行；统一放在子命令后面，这样也不会落到用户参数的 `--` 后面。
- kube 证书写在 `keys/teleport.dify.dev/yangyonggang-kube/teleport.dify.dev/<集群>.cred`。

**审计归属**：两个工具的所有访问在 CloudTrail 和 Teleport 审计里都记在 `yangyonggang` 名下。

## 4. 方案对比

| 方案 | 做法 | 取舍 |
| --- | --- | --- |
| **A（选定）** | db-gateway 进程内用子进程跑 `tsh aws …` 和 `tsh kubectl …`，按需登录 | 不加容器、不加密钥、不改 watchdog；命令与手工排查一致，可在服务器上原样复现；每次调用有 1–4 秒启动开销 |
| B | 像数据库隧道那样常驻 `tsh proxy aws` / `tsh proxy kube` 容器，网关用 SDK 连本地端口 | 单次更快；但要新增不在 CI 管理范围内的容器，续期后还要重启它们，改动和故障点都多 |
| C | 直接用 AWS 访问密钥和集群 kubeconfig | 排除：没有这类凭据，且绕开 Teleport 审计 |

在 A 之内为了少改动做的取舍：
- **不做 `plugin_lambda_resolve` 工具。** `plugin_lambda_logs` 直接接受 `tenant_id + plugin_id`，网关内部用一条 SQL 查出函数名；需要看全部版本和 checksum 时，用现有 `db_gateway_query` 跑工具描述里给出的 SQL。
- **不加 HTTP 端点和 stdio 转发。** 新工具只走 `/mcp`；`mcp_schema.TOOLS` 保持为两个旧工具，stdio 脚本不用改。
- **`kube_read` 直接透传 kubectl 参数**，不为每种资源单独设计参数。

Task 0 之后在 A 之内新增的两个决定：
- **镜像自带 tsh 18.11.0**（`/opt/teleport/bin/tsh`，固定版本并校验 sha256），只给新工具用。宿主机挂载的 `/usr/local/bin/tsh` 18.0.0 不动，现有代码继续用它。不升级宿主机 tsh 的原因：隧道容器和 watchdog 也在用，不在本仓库部署范围。
- **影子 `TELEPORT_HOME`**：新工具从不写线上 `.tsh`。每次调用前把线上目录（不含 `-app`、`-kube`、`-db`、`bin`）同步到容器内的 `/tmp/db-gateway/tsh-home`，tsh 18.11 只在这个副本里读写。这样做的效果：
  - 连接池监视看不到任何变化，`pg_pool.py` 不用改；
  - 18.11 不会改写 18.0.0 的隧道和 watchdog 正在读的文件；
  - 不会与 watchdog 续期互相覆盖；
  - 续期的 10 秒窗口里，线上 `keys/` 暂时为空，影子里的旧证书还在有效期内，工具照常可用。

## 5. 设计

### 5.1 tsh 执行层（新模块 `teleport_cli.py`；两个工具各一个模块 `lambda_logs.py`、`kube_read.py`）

**影子 TELEPORT_HOME**（`ensure_shadow_home(cfg)`，每次工具调用开始时调用一次）
- 源目录 `teleport.auth_home`，副本 `teleport_tools.shadow_home`。
- 源签名：`keys/*/*.crt`（只看 proxy 目录下一层的文件，即用户 TLS 证书）的相对路径和 sha256。续期时证书内容变化，tsh 重写时内容不变，所以签名只随续期变化。
- 签名与副本里记录的相同 → 直接用副本。
- 不同 → 在进程内锁下复制到临时目录，忽略名称为 `bin` 或以 `-app`、`-kube`、`-db` 结尾的目录；复制后再算一次源签名，一致才写入签名文件并整体替换副本（rename），否则重试一次。
- 源目录没有证书（续期窗口）或复制失败 → 副本存在就继续用副本，不存在就抛 `TeleportAuthExpired`。
- 副本里没有 app/kube 证书时，tsh 18.11 自动签发，不需要额外处理。

**子进程**（`run_tsh(args, *, cfg, deadline, max_output_bytes=None)`）
- 可执行文件为 `teleport_tools.tsh_path`。参数列表方式执行，不经 shell。
- 环境：继承网关环境，删除 `HTTP(S)_PROXY`、`ALL_PROXY`、`NO_PROXY` 的大小写形式，设置以下三项：
  - `TELEPORT_HOME=<shadow_home>`
  - `KUBECONFIG=<kube.kubeconfig_path>`
  - `TELEPORT_TOOLS_VERSION=off`：关闭 tsh 客户端自动更新，防止改用别的版本。
  
  不设 `NO_PROXY=*`：`tsh aws` 要靠 `HTTPS_PROXY` 让 aws 子进程走它的本地代理。
- `start_new_session=True`：到截止时间后 `killpg` 杀掉整个进程组，再对读管道的任务最多等 2 秒。实测 `tsh aws` 会起一个 aws 子进程，只杀 tsh 时管道还要再开 3.7 秒。
- 边读边收：stdout 只保留前 `max_output_bytes` 字节但继续读完；stderr 保留 64 KiB；返回前去掉 ANSI 颜色码。
- 截止时间已过时不再启动子进程，直接返回 `timed_out`。

**统一截止时间**：每次工具调用开始时算一次 `deadline = now + time_budget_seconds`，SQL 定位、登录、每次 tsh 调用都用剩余时间，不设「至少 N 秒」的下限。登录超时取 `min(20, 剩余)`。这样总耗时不超过 `time_budget_seconds`（默认 40）加少量开销。

**按需登录**（`run_tsh_with_login`、`tsh_login`）
- 命令失败且 `needs_login(result)` 为真时，在进程内锁下执行一次登录：日志用 `tsh apps login --aws-role <角色> <app>`，集群用 `tsh kube login --all`。成功后用剩余时间重试一次。
- 有 `not_before`：同参数的登录在本次调用开始后已经成功过，就不再登录。并发调用共享一次登录。
- 登录失败时用宿主机 tsh 对**源目录**跑 `tsh status`：未登录 → `TELEPORT_EXPIRED`（等 watchdog 续期，调用方重试）；已登录 → `NOT_AUTHORIZED`，附 stderr 和当前 Roles。
- 18.11 下 tsh aws 会自动登录 app，kube 证书也会自动重签，所以这条路径只是兜底。

### 5.2 连接池监视

不改。新工具只写影子目录，线上 `keys/` 没有变化。

### 5.3 工具 `plugin_lambda_logs`

输入
- 定位函数，二选一：
  - `function_name`：直接给函数名（含 `--2` 等后缀）。格式 `^[A-Za-z0-9_-]{1,64}$`，不要求在插件库里存在，已下线版本的历史日志也能查。
  - `tenant_id` + `plugin_id`（`publisher/name`）：网关查 `plugin_installations` LEFT JOIN `serverless_runtimes` 得到函数名。没有安装记录、没有运行映射或后端不是 Lambda us-east-1 时返回 `UNKNOWN_FUNCTION` 并说明是哪一种。
- 时间窗口，二选一：
  - `start_time` + `end_time`：ISO 8601 或毫秒时间戳；不带时区按北京时间；`end_time` 不含端点。数字小于 1e11 时多半是秒级时间戳，直接 `INVALID_ARGUMENT` 提示改用毫秒。
  - `since_minutes`：正整数，无上限。
- `filter_pattern`（可选）：CloudWatch 过滤语法原样透传。
- `log_stream_names`（可选）。
- `limit`：默认 1000，最大 10000。
- `next_token`：翻页令牌。

执行
- `tsh aws --app aws-support-logs logs filter-log-events --region us-east-1 --log-group-name /aws/lambda/<fn> …`，按页取。`--filter-pattern=`、`--next-token=` 用等号写法。
- 时间预算默认 40 秒：到时间或达到 `limit` 就返回已取到的事件和 `next_token`，不报超时。Nginx 的 `/db-gateway/` 路由 `proxy_read_timeout` 是 60 秒（实测）。
- 实测单页约 1.4–1.9 秒；首次（需要自动登录 app）约 4.5 秒。

输出
- `function_name`、`log_group`；按 `tenant_id + plugin_id` 定位时另带 `plugin_unique_identifier`，并提示「安装表反映当前状态，故障后升级或重装过要按故障时的版本查」。
- 实际窗口（UTC 和北京时间各一份）。
- `events[]`：`timestamp`、`log_stream`、`event_id`、`message`（原样，不截断）。
- `event_count`、`next_token`、`complete`（是否已取全）、`warnings`（含空结果时的排查提示）。

工具描述里附文档中的两条 SQL（按版本查、按工作空间查），供调用方用 `db_gateway_query` 查全部版本和 checksum。

### 5.4 工具 `kube_read`

输入
- `cluster`：配置里的别名，默认提供 `prod`（`lg-eks-01`）和 `staging`（`dify-plugin-1`）。
- `args`：kubectl 参数数组（不含 `kubectl` 本身），例如
  - `["get","deploy","-n","dify-plugin","-o","wide"]`（看生产部署的镜像版本和副本数）
  - `["get","pods","-n","dify-plugin","-l","app=dify-api-oltp"]`
  - `["describe","pod","<名称>","-n","dify-plugin"]`
  - `["logs","deploy/dify-api-oltp","-n","dify-plugin","--since=10m","--tail=500"]`

校验
- 第一个参数必须是只读子命令：`get`、`describe`、`logs`、`top`、`explain`、`api-resources`、`api-versions`、`version`、`auth can-i`。
- 拒绝四类参数（防止误操作和换身份，不是安全边界）：
  - 读本地文件：`-f`、`--filename`、`-k`、`--kustomize`
  - 换身份或换目标：`--kubeconfig`、`--context`、`--cluster`、`--user`、`--as*`、`--token`、`--server`/`-s`、证书和 TLS 相关参数
  - 持续输出：`--follow`、`-w`、`--watch`、`--watch-only`
  - 改写缓存位置：`--cache-dir`
- 其余参数不限制。能读哪些资源由 Teleport 角色决定，被拒绝时原样返回 Teleport 的错误。

执行
- `tsh kubectl <子命令> --context <该集群的 context> <其余参数>`。
- 时间预算默认 40 秒。

结果判定（`tsh kubectl` 退出码永远是 0）
- stderr 里有以 `error:`、`Error from server`、`Error in configuration`、`Unable to connect to the server` 开头的行，或退出码非 0 → `success=false`、`KUBECTL_ERROR`，`output` 照常返回。
- `No resources found …` 之类的提示不算错误。

输出
- `output`（kubectl 标准输出原文）、`stderr`、`exit_code`。
- 输出超过 `max_output_bytes`（默认 1 MB，可配置）时保留开头并标 `truncated: true`，提示用 `--tail`、`--since`、`--since-time`、`-l` 缩小范围；到时间预算被中止时标 `timed_out: true` 并返回已读到的部分。

### 5.5 配置（`deploy/runtime.json` → `settings.teleport_tools`，不新增 Secret）

| 键 | 默认 |
| --- | --- |
| `tsh_path` | `/opt/teleport/bin/tsh`（镜像自带的 18.11.0） |
| `shadow_home` | `/tmp/db-gateway/tsh-home`（容器重启后丢失，首次调用时重新同步） |
| `lambda_logs.enabled` | `true`；`false` 时工具不出现在 `tools/list` |
| `lambda_logs.app` / `aws_role` / `region` | `aws-support-logs` / `SupportLambdaLogsReadOnly` / `us-east-1` |
| `lambda_logs.default_timezone` | `Asia/Shanghai` |
| `lambda_logs.default_limit` / `max_limit` | 1000 / 10000 |
| `kube.enabled` | `true` |
| `kube.clusters` | `{"prod": "lg-eks-01", "staging": "dify-plugin-1"}`（值为 Teleport 里的集群名，context 名在运行时从 kubeconfig 里匹配） |
| `kube.max_output_bytes` | 1048576 |
| `kube.kubeconfig_path` | `/tmp/db-gateway/kubeconfig`（容器重启后丢失，首次调用时按需重新生成） |
| `time_budget_seconds` | 40（整次工具调用的预算） |
| `max_concurrent` | 3 |

### 5.6 MCP 与审计

- 两个工具的 schema 写在 `mcp_schema.py`，`readOnlyHint=true`、`destructiveHint=false`（OpenConnector 据此判定为 read，不要求确认）。`mcp.py` 增加分发。
- 审计写 `db_gateway_audit.log`：工具名、状态、耗时；日志工具记函数名、窗口、过滤条件、返回事件数、`complete`；`kube_read` 记集群、完整参数、退出码、输出字节数。**不记录日志和输出内容**。
- 错误码：沿用 `INVALID_ARGUMENT`、`TELEPORT_EXPIRED`、`QUERY_ERROR`；新增 `UNKNOWN_FUNCTION`、`NOT_AUTHORIZED`、`AWS_ERROR`、`KUBECTL_ERROR`（stderr 有错误行或非零退出，附 stderr）、`LOGS_TIMEOUT`（第一页就超过时间预算，一条都没取到时）。

## 6. 改动清单

| 文件 | 改动 |
| --- | --- |
| `src/db_gateway/teleport_cli.py` | 新增：影子 TELEPORT_HOME、tsh 执行层、按需登录 |
| `src/db_gateway/lambda_logs.py`、`kube_read.py` | 新增：两个工具的实现 |
| `src/db_gateway/mcp_schema.py`、`mcp.py` | 新增两个工具的 schema 和分发 |
| `src/db_gateway/config.py` | 新增 `teleport_tools` 配置 |
| `Dockerfile` | 加 tsh 18.11.0（固定版本、校验 sha256，放在 `/opt/teleport/bin/tsh`） |
| `requirements.runtime.txt`、`pyproject.toml`、`uv.lock` | 加 aws CLI v1（固定版本） |
| `deploy/runtime.json`、`config.example.yaml` | 加 `settings.teleport_tools` |
| `README.md`、`tests/` | 文档和测试 |

不改：端口、网络、挂载、Secret、`pg_pool.py`、其他服务目录、watchdog、隧道容器、宿主机 tsh。

## 7. 测试

单元测试（tsh 用假脚本，不用生产凭据）：
- 影子目录：首次复制且排除 `-app`/`-kube`/`-db`/`bin`；签名不变不重复复制；源证书变化后重新同步；续期窗口（源目录没有证书）继续用旧副本；没有副本时 `TeleportAuthExpired`；运行后源目录文件不变。
- 执行层：参数列表无 shell、环境变量（含删除代理变量、`TELEPORT_TOOLS_VERSION=off`）、截止时间到期返回部分输出、杀进程组后孙进程也退出、截止时间已过不启动、按需登录后重试、并发只登录一次、错误分类、ANSI 去除。
- `plugin_lambda_logs`：两种定位方式、时间解析（带时区、不带时区按北京时间、毫秒、`since_minutes`、秒级时间戳被拒）、翻页合并、`limit` 和预算提前返回带 `next_token`、取全时 `complete=true`、长消息不截断、`--filter-pattern=`/`--next-token=` 写法。
- `kube_read`：只读子命令白名单、四类被拒参数（含 `--as=x`、`-f` 等写法变体）、`--context` 紧跟子命令、集群别名、退出码 0 但 stderr 有错误时判为 `KUBECTL_ERROR`、`No resources found` 不算错误、输出截断和超时标记。
- MCP：`tools/list` 含新工具及 annotations；`enabled=false` 时隐藏；stdio 仍只列旧工具。

## 8. 上线与验证

- 只改 `db-gateway/`（文档在根目录 `docs/`），CI 只选中 db-gateway。
- 上线后：
  1. OpenConnector `dify-db` 的 `list_tools` 出现 4 个工具。
  2. `kube_read(prod, ["get","deploy","-n","dify-plugin","dify-api-oltp","-o","wide"])` 返回镜像和副本数；`["get","secrets","-n","dify-plugin"]` 返回 `KUBECTL_ERROR` 和 Teleport 的 Forbidden；`["delete","pod","x"]` 被网关拒绝。
  3. `plugin_lambda_logs`：文档示例函数 `9a442b2d0d9b43e56a190df6ca7dc207` 查近期窗口加 `"REPORT RequestId"` 有结果；跨多天窗口加稀疏过滤能分批取到 `complete=true`。
  4. 工具调用前后，线上 `/opt/dify-db/.tsh/keys/` 的文件列表和 mtime 不变（没有 `-app`、`-kube` 目录）；db-gateway 日志里没有随之出现 `teleport keys changed`，`db_gateway_query` 正常。
  5. 下一次 watchdog 续期后两个工具无需人工即可恢复。
  6. 其他 MCP 容器、`tsh-proxy-*`、`tsh-auth-watchdog` 的容器 ID 和启动时间不变。

## 9. 前提复核结果

### 9.1 角色（2026-11-27 19:32 新加坡时间 / 20:32 KST）

- watchdog 在 19:28（新加坡时间）自动续期成功，没有人工操作。
- 新证书的 Roles：`production-read-all, staging-read-all, support-lambda-logs`。
- `tsh apps ls` 可见 `aws-support-logs`（HTTP 类型，AWS 账号 5232866…）。

这个角色直接绑在账号上，不需要 access request，自动续期会一直带上它，`plugin_lambda_logs` 可以默认启用。

### 9.2 Task 0（2026-11-27 20:45–21:00 KST，首尔服务器）

做法：把 `/opt/dify-db/.tsh` 复制到 `/root/tsh-spike-20261127`，在一次性容器里跑（db-gateway 当前镜像、`--network host`、临时 venv 装 awscli）。线上 `.tsh`、db-gateway 和 tsh 相关容器都没动；结束后删除了临时目录（含私钥副本）。

| 项目 | 结果 |
| --- | --- |
| awscli v1 | `pip install awscli` 得到 1.46.3（内置 botocore 1.43.62），依赖 colorama 0.4.6、docutils 0.19、jmespath 1.1.0、pyasn1 0.6.5、python-dateutil 2.9.0.post0、PyYAML、rsa 4.7.2、six 1.17.0、urllib3 2.8.0 |
| tsh 18.0.0 + `tsh aws` | 全部 403，空 body；`tsh --debug` 显示本地代理 `AWS signature verification failed`。awscli 1.36.40 / 1.37.26 / 1.40.45 / 1.43.15 / 1.45.64 / 1.46.3 和 v2.42.4 结果相同 |
| tsh 18.11.0 + `tsh aws` | 正常。1 小时窗口 `"REPORT RequestId"` 取到 3 条带 `nextToken`，1.4 秒；`--next-token=` 续页正常；3 天窗口 `"Traceback"` 一页 32 条带 `nextToken`，1.9 秒 |
| 没有 app 证书时 `tsh aws`（18.11） | 自动登录 app 后成功，rc=0，4.2–4.6 秒 |
| `tsh apps login`（18.11） | 2.2–2.4 秒；`--aws-role` 写错时仍以唯一可用角色登录成功 |
| 不存在的 app | `ERROR: app "…" not found, use \`tsh apps ls\` to see registered apps`，rc=1 |
| 不存在的日志组 | `An error occurred (ResourceNotFoundException) when calling the FilterLogEvents operation: The specified log group does not exist.`，tsh 再加一行 `ERROR: exit status 255`，rc=1 |
| `--filter-pattern -DEBUG` | `aws: error: argument --filter-pattern: expected one argument`；`--filter-pattern=-DEBUG` 正常 |
| 没有 profile（续期窗口的状态） | `tsh aws`、`tsh apps login`、`tsh kube login` 都是 `ERROR: No proxy address specified, missed --proxy flag?`，rc=1，0.08 秒 |
| 进程结构 | `tsh aws`（pid A）→ `python …/aws`（父进程 A，同一进程组）。只 SIGKILL tsh，stdout 管道在 3.7 秒后才关闭 |
| keys/ 写入 | 每次 apps/aws/kube 操作都重写 `keys/teleport.dify.dev/` 下的 `yangyonggang`、`.crt`、`.key`、`.pub`、`certs.pem`、`cas/…pem`、`yangyonggang-ssh/…-cert.pub`；`.crt`、`.key`、CA 内容不变，SSH 私钥 `yangyonggang` 内容改变；另建 `yangyonggang-app/teleport.dify.dev/aws-support-logs{.crt,.key,-localca.pem}` 和 `yangyonggang-kube/teleport.dify.dev/<集群>.cred`。profile yaml 不变 |
| `tsh kube ls` / `kube login --all` | 两个集群；login 1.9 秒，生成两个 context；kubeconfig 的 exec 带 `TELEPORT_HOME=<登录时的目录>`；`KUBECONFIG` 的父目录不存在时 tsh 会自己创建 |
| `tsh kubectl get --context <prod> deploy dify-api-oltp -n dify-plugin -o wide` | 成功（7/7，镜像 `langgenius/dify-api:hotfix-1.20.0-fix.3@sha256:…`），3.4 秒 |
| `-l` 透传 | 交给了 kubectl（`No resources found in dify-plugin namespace.`，该部署实际用 `workload.user.cattle.io/workloadselector` 标签） |
| `auth --context <prod> can-i get pods` | `yes` |
| 删除 `-kube` 目录后再 `tsh kubectl` | 自动重签，成功，3.0 秒 |
| kubectl 退出码 | Forbidden、NotFound、`unknown flag`、`context was not found` 都是 rc=0（18.0.0 和 18.11.0 相同）；没有 kubeconfig 时 `error: context "…" does not exist`，rc=0 |

## 10. 留给实现者

- 以下数值可按实测调整：时间预算、单页事件数、并发数、读管道的宽限时间。
- `KUBE_LOGIN_MARKERS` 只能在线上真实遇到凭据错误时校正（Task 0 里删除 kube 证书后会自动重签，没有复现出凭据错误）。

## 11. Claude 做的决定（用户可改）

- 方案 A：进程内子进程跑 tsh，不加常驻容器。
- 把只读查看集群（`kube_read`）一起做进来；用户原话是「看看能不能读取生产集群的信息」，实测可以，且与日志工具共用同一个执行层。
- 不做单独的 resolve 工具、HTTP 端点和 stdio 转发。
- `kube_read` 透传 kubectl 参数，只限制子命令和四类参数；不限制 `-o` 的输出格式（用户确认单人使用，不需要防读本地文件）。
- 日志：时间不带时区按北京时间；不设时间跨度上限；默认 1000 条、最多 10000 条；内容不截断；40 秒时间预算到期返回部分结果和翻页令牌。
- `kube_read` 输出上限 1 MB，超过时明确标记截断。
- 审计不记日志和输出内容。
- Task 0 之后：镜像自带 tsh 18.11.0 给新工具用；新工具使用影子 `TELEPORT_HOME`，不写线上 `.tsh`，因此不改 `pg_pool.py`；`kube_read` 按 stderr 判定错误。
