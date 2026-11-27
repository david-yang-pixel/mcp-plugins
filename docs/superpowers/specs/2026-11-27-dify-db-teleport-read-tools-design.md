# dify-db：通过 Teleport 只读查询插件 Lambda 日志和集群信息 设计

日期：2026-11-27
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

## 2. 目标、非目标、反目标

目标
- 新增两个只读工具：`plugin_lambda_logs`、`kube_read`。
- 日志能查全：不设时间跨度上限、不截断日志内容，任何范围都能靠翻页取完。
- 证书续期后两个工具自动恢复，不需要人工登录。
- 改动只落在 `db-gateway/` 目录：一个新模块、工具 schema 和分发、镜像加 aws CLI、连接池监视改一处。

非目标
- 实时跟踪（`--follow`、`--watch`）。MCP 是请求-响应。
- 任何写操作（exec、delete、patch、apply 等）。
- 修改 `tsh-auth-watchdog`、`tsh-proxy-*`（AGENTS.md 规定不在本仓库部署范围）。
- 单独的访问控制；接入 Teleport 里能看到的其他数据库（KnowledgeFS 生产只读库、Redshift `saas-02` 等）。
- 单独的 `plugin_lambda_resolve` 工具和 HTTP 端点（见 §4）。

反目标
- 静默丢日志：截断内容、超出部分不给翻页令牌、范围大就直接超时而什么都拿不到。
- 登录 app 或 kube 时打断数据库连接池。
- 证书续期后新工具静默失效。
- `kube_read` 能被用来做写操作、读网关容器里的本地文件、或换一个身份访问。

## 3. 现状与实测

**Teleport 会话**（2026-11-27 实测，首尔服务器，`TELEPORT_HOME=/opt/dify-db/.tsh`）
- 账号 `yangyonggang`，角色 `production-read-all, staging-read-all`；tsh 客户端 v18.0.0，proxy 18.11.0。
- db-gateway 容器：host 网络、root、挂载 `/usr/local/bin/tsh` 和 `/opt/dify-db/.tsh → /opt/zd-agent/.tsh`。
- watchdog 续期时 `rmtree keys/` 后重新 `tsh login`（约 10 秒）。
- `pg_pool.keys_signature` 监视整个 `keys/`，任何文件变化都会关闭三个库的连接池。

**Lambda 日志**
- 需要角色 `support-lambda-logs`，app `aws-support-logs`，AWS 角色 `SupportLambdaLogsReadOnly`。
- 19:17 核对时证书（07:56 签发）还没有这个角色；19:28 自动续期后已带上，`tsh apps ls` 可见 `aws-support-logs`（见 §9）。
- 镜像里没有 aws CLI，`tsh aws` 需要它。
- 插件库：`serverless_runtimes` 18,115 行，全部是 Lambda us-east-1；`function_name` 为 32 位十六进制，44 行带 `--N` 后缀；`plugin_installations` 有 `(tenant_id, plugin_id)` 唯一索引，所以「工作空间 + 插件」最多对应一条安装记录。

**Kubernetes**（用会话的临时副本实测，没有动线上的 `.tsh`）
- 可见两个集群：
  - `lg-eks-01`（生产，AWS 账号 523286616386）：`dify-plugin` 命名空间 262 个 Pod，是生产 SaaS 主体（`dify-api-oltp`、各 worker、`dify-gateway`、knowledge-fs、marketplace 等）；另有 `tokener`、`argocd`、`kube-system` 等。
  - `dify-plugin-1`（账号 014498624057）：`dify-staging` 95 个 Pod，另有 `dify-marketplace` 等。
- 当前角色能读：`pods`、`pods/log`、`deployments`。
- 被 Teleport 拒绝：`events`、`configmaps`、`secrets`、`nodes`、`services`、`statefulsets`、`replicasets`、`daemonsets`、`ingresses`、`hpa`、`jobs`、`pods/exec`、删除和修改。`top` 不可用（没有 Metrics API）。
- `tsh kubectl` 内置 kubectl（v1.33.0），不需要另装；一次调用约 1 秒。
- `tsh kube login --all` 生成含两个 context 的 kubeconfig（`teleport.dify.dev-lg-eks-01-us-east-1-523286616386`、`teleport.dify.dev-dify-plugin-1-eks-us-east-1-014498624057`），凭据由 kubeconfig 里的 `tsh kube credentials` 按需签发。
- `--context` 必须放在 kubectl 子命令之后（`tsh kubectl get … --context X`）；放在前面会被 tsh 当成自己的参数报错。
- kube 相关文件写在 `keys/teleport.dify.dev/yangyonggang-kube/` 下。

**审计归属**：两个工具的所有访问在 CloudTrail 和 Teleport 审计里都记在 `yangyonggang` 名下。共用密钥意味着能调 dify-db 的调用方都以该身份读插件日志和生产 Pod 日志（可能含客户数据）。

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

## 5. 设计

### 5.1 tsh 执行层（新模块 `teleport_cli.py`；两个工具各一个模块 `lambda_logs.py`、`kube_read.py`）

一个函数负责跑 `tsh <参数列表>`：
- 参数列表方式执行，不经 shell。
- 环境：`TELEPORT_HOME=<teleport.auth_home>`；`KUBECONFIG=<网关自己的文件>`（放在 `keys/` 之外，例如审计日志所在的数据目录，watchdog 续期不会删它）；清除 `HTTP(S)_PROXY`/`ALL_PROXY`（与 `teleport_login.py` 同理）。
- 边读边收集输出，带时间预算；到时间杀掉进程并返回已读到的部分。
- 每个工具各自的并发上限（默认 3），不占用数据库查询的槽位。
- 按需登录：命令失败且错误属于「没有 app / kube 凭据」时，在进程内锁下执行一次登录（`tsh apps login --aws-role <角色> <app>` 或 `tsh kube login --all`），成功后重试一次；并发调用共享这一次登录。
- 错误分类：用户证书过期或未登录 → `TELEPORT_EXPIRED`（等 watchdog 续期，调用方重试）；角色不允许 → `NOT_AUTHORIZED`，附 `tsh status` 的 Roles。

### 5.2 连接池监视

`keys_signature` 遍历 `keys/` 时跳过名称以 `-app` 或 `-kube` 结尾的目录。用户证书和 db 证书变化仍然触发清空。

### 5.3 工具 `plugin_lambda_logs`

输入
- 定位函数，二选一：
  - `function_name`：直接给函数名（含 `--2` 等后缀）。格式 `^[A-Za-z0-9_-]{1,64}$`，不要求在插件库里存在，已下线版本的历史日志也能查。
  - `tenant_id` + `plugin_id`（`publisher/name`）：网关查 `plugin_installations` LEFT JOIN `serverless_runtimes` 得到函数名。没有安装记录、没有运行映射或后端不是 Lambda us-east-1 时返回 `UNKNOWN_FUNCTION` 并说明是哪一种。
- 时间窗口，二选一：
  - `start_time` + `end_time`：ISO 8601 或毫秒时间戳；不带时区按北京时间；`end_time` 不含端点。
  - `since_minutes`：正整数，无上限。
- `filter_pattern`（可选）：CloudWatch 过滤语法原样透传。
- `log_stream_names`（可选）。
- `limit`：默认 1000，最大 10000。
- `next_token`：翻页令牌。

执行
- `tsh aws --app aws-support-logs logs filter-log-events --region us-east-1 --log-group-name /aws/lambda/<fn> …`，按页取。
- 时间预算默认 40 秒：到时间或达到 `limit` 就返回已取到的事件和 `next_token`，不报超时。Nginx 的 `/db-gateway/` 路由 `proxy_read_timeout` 是 60 秒（实测），预算加上一次按需登录必须留在 60 秒以内。

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
- 拒绝四类参数：
  - 读本地文件：`-f`、`--filename`、`-k`、`--kustomize`
  - 换身份或换目标：`--kubeconfig`、`--context`、`--cluster`、`--user`、`--as*`、`--token`、`--server`/`-s`、证书和 TLS 相关参数
  - 持续输出：`--follow`、`-w`、`--watch`、`--watch-only`
  - 改写缓存位置：`--cache-dir`
- 其余参数不限制。能读哪些资源由 Teleport 角色决定，被拒绝时原样返回 Teleport 的错误。

执行
- `tsh kubectl <args> --context <该集群的 context>`（`--context` 追加在最后）。
- 时间预算默认 40 秒。

输出
- `output`（kubectl 标准输出原文）、`stderr`、`exit_code`。
- 输出超过 `max_output_bytes`（默认 1 MB，可配置）时保留开头并标 `truncated: true`，提示用 `--tail`、`--since`、`--since-time`、`-l` 缩小范围；到时间预算被中止时标 `timed_out: true` 并返回已读到的部分。

### 5.5 配置（`deploy/runtime.json` → `settings.teleport_tools`，不新增 Secret）

| 键 | 默认 |
| --- | --- |
| `lambda_logs.enabled` | `true`；`false` 时工具不出现在 `tools/list` |
| `lambda_logs.app` / `aws_role` / `region` | `aws-support-logs` / `SupportLambdaLogsReadOnly` / `us-east-1` |
| `lambda_logs.default_timezone` | `Asia/Shanghai` |
| `lambda_logs.default_limit` / `max_limit` | 1000 / 10000 |
| `kube.enabled` | `true` |
| `kube.clusters` | `{"prod": "lg-eks-01", "staging": "dify-plugin-1"}`（值为 Teleport 里的集群名，context 名在运行时从 kubeconfig 里匹配） |
| `kube.max_output_bytes` | 1048576 |
| `kube.kubeconfig_path` | `/tmp/db-gateway/kubeconfig`（容器重启后丢失，首次调用时按需重新生成） |
| `time_budget_seconds` | 40 |
| `max_concurrent` | 3 |

### 5.6 MCP 与审计

- 两个工具的 schema 写在 `mcp_schema.py`，`readOnlyHint=true`、`destructiveHint=false`（OpenConnector 据此判定为 read，不要求确认）。`mcp.py` 增加分发。
- 审计写 `db_gateway_audit.log`：工具名、状态、耗时；日志工具记函数名、窗口、过滤条件、返回事件数、`complete`；`kube_read` 记集群、完整参数、退出码、输出字节数。**不记录日志和输出内容**。
- 错误码：沿用 `INVALID_ARGUMENT`、`TELEPORT_EXPIRED`；新增 `UNKNOWN_FUNCTION`、`NOT_AUTHORIZED`、`AWS_ERROR`、`KUBECTL_ERROR`（非零退出，附 stderr）、`LOGS_TIMEOUT`（第一页就超过时间预算，一条都没取到时）。

## 6. 改动清单

| 文件 | 改动 |
| --- | --- |
| `src/db_gateway/teleport_cli.py` | 新增：tsh 执行层、按需登录 |
| `src/db_gateway/lambda_logs.py`、`kube_read.py` | 新增：两个工具的实现 |
| `src/db_gateway/mcp_schema.py`、`mcp.py` | 新增两个工具的 schema 和分发 |
| `src/db_gateway/config.py` | 新增 `teleport_tools` 配置 |
| `src/db_gateway/pg_pool.py` | `keys_signature` 跳过 `-app`、`-kube` 目录 |
| `Dockerfile`、`requirements.runtime.txt`、`pyproject.toml`、`uv.lock` | 加 aws CLI（固定版本） |
| `deploy/runtime.json` | 加 `settings.teleport_tools` |
| `README.md`、`tests/` | 文档和测试 |

不改：端口、网络、挂载、Secret、其他服务目录、watchdog、隧道容器。

## 7. 测试

单元测试（tsh 用假脚本，不用生产凭据）：
- 执行层：参数列表无 shell、环境变量、时间预算到期返回部分输出、按需登录后重试、并发只登录一次、错误分类。
- `plugin_lambda_logs`：两种定位方式、时间解析（带时区、不带时区按北京时间、毫秒、`since_minutes`）、翻页合并、`limit` 和预算提前返回带 `next_token`、取全时 `complete=true`、长消息不截断。
- `kube_read`：只读子命令白名单、四类被拒参数（含 `--as=x`、`-f` 等写法变体）、`--context` 追加在最后、集群别名、输出截断和超时标记。
- `keys_signature`：`-app`、`-kube` 目录变化不触发清空，用户证书变化仍触发。
- MCP：`tools/list` 含新工具及 annotations；`enabled=false` 时隐藏；stdio 仍只列旧工具。

## 8. 上线与验证

- 只改 `db-gateway/`，CI 只选中 db-gateway。
- 上线后：
  1. OpenConnector `dify-db` 的 `list_tools` 出现 4 个工具。
  2. `kube_read(prod, ["get","deploy","-n","dify-plugin","dify-api-oltp","-o","wide"])` 返回镜像和副本数；`["get","secrets","-n","dify-plugin"]` 返回 Teleport 的拒绝；`["delete","pod","x"]` 被网关拒绝。
  3. `plugin_lambda_logs`：文档示例函数 `9a442b2d0d9b43e56a190df6ca7dc207` 查近期窗口加 `"REPORT RequestId"` 有结果；跨多天窗口加稀疏过滤能分批取到 `complete=true`。
  4. 首次调用触发登录后，db-gateway 日志里没有随之出现 `teleport keys changed`，`db_gateway_query` 正常。
  5. 下一次 watchdog 续期后两个工具无需人工即可恢复。
  6. 其他 MCP 容器、`tsh-proxy-*`、`tsh-auth-watchdog` 的容器 ID 和启动时间不变。

## 9. 前提复核结果

2026-11-27 19:32（新加坡时间）复核，前提成立：

- watchdog 在 19:28 自动续期成功，没有人工操作。
- 新证书的 Roles：`production-read-all, staging-read-all, support-lambda-logs`。
- `tsh apps ls` 可见 `aws-support-logs`（HTTP 类型，AWS 账号 5232866…）。

所以这个角色是直接绑在账号上的，不需要 access request，自动续期会一直带上它，`plugin_lambda_logs` 可以默认启用。尚未验证的部分：`tsh apps login --aws-role SupportLambdaLogsReadOnly aws-support-logs` 和 `tsh aws logs …` 在服务器上的实际执行（服务器还没有 aws CLI），留到上线校验。

## 10. 留给实现者

- aws CLI 的安装方式（pip `awscli` 固定版本进锁文件，或官方 v2 安装包加校验）。
- 「没有 app / kube 凭据」「角色不允许」的错误匹配串，以 tsh 18.0.0 的实际输出为准。
- 续期清空 `keys/` 后，kubeconfig 里的 `tsh kube credentials` 能否直接重新签发；不能的话在失败时重新 `tsh kube login --all`。
- Lambda 日志按页取的实现方式（逐页 `--next-token` 或等效做法），注意每次 `tsh aws` 有启动开销。
- 各默认数值可按实测调整。

## 11. Claude 做的决定（用户可改）

- 方案 A：进程内子进程跑 tsh，不加常驻容器。
- 把只读查看集群（`kube_read`）一起做进来；用户原话是「看看能不能读取生产集群的信息」，实测可以，且与日志工具共用同一个执行层。
- 不做单独的 resolve 工具、HTTP 端点和 stdio 转发。
- `kube_read` 透传 kubectl 参数，只限制子命令和四类危险参数。
- 日志：时间不带时区按北京时间；不设时间跨度上限；默认 1000 条、最多 10000 条；内容不截断；40 秒时间预算到期返回部分结果和翻页令牌。
- `kube_read` 输出上限 1 MB，超过时明确标记截断。
- 审计不记日志和输出内容。
