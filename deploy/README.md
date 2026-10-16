# GitHub Secrets 与生产部署

仓库：`david-yang-pixel/mcp-plugins`。生产环境：腾讯云 `zendesk-agent-server`。

## 修改密钥

在仓库 **Settings → Secrets and variables → Actions** 修改对应 Secret。每个值独立保存，命名为 `MCP_<服务名大写、横线改下划线>__<环境变量名>`。

| 服务 | Secret 前缀 | 首次导入的非空值数量 |
| --- | --- | ---: |
| db-gateway | `MCP_DB_GATEWAY__` | 6 |
| signoz-mcp | `MCP_SIGNOZ_MCP__` | 2 |
| source-mcp | `MCP_SOURCE_MCP__` | 3 |
| github-mcp | `MCP_GITHUB_MCP__` | 2 |
| docs-tool-service | `MCP_DOCS_TOOL_SERVICE__` | 1 |
| notification-service | `MCP_NOTIFICATION_SERVICE__` | 2 |
| zendesk-mcp | `MCP_ZENDESK_MCP__` | 3 |
| zendesk-browser-relay | `MCP_ZENDESK_BROWSER_RELAY__` | 9 |

具体变量与 Secret 的映射位于各服务的 `deploy/runtime.json`。`required: false` 表示线上原来为空，未创建空 Secret；以后可以在 GitHub 添加同名值。Browser Relay 的 9 项包含文件形式的飞书 Webhook，部署时以只读文件挂载。

修改 Secrets 不会触发 GitHub 的 push 事件。更新完成后进入 **Actions → MCP CI and production deployment → Run workflow**，选择 `main`，在 `service` 输入对应目录名，或输入 `all`。工作流自动测试、构建并部署，不需要再登录服务器手工修改 `.env`。同一把密钥在其他服务或外部调用方也被使用时，应协调更新这些使用方；本仓库不会修改 Zendesk Agent 或其他仓库的配置。

## 代码与普通配置

非敏感运行配置保存在各服务的 `deploy/runtime.json`，DB 的原生产 YAML 以等价 JSON 存在 `settings` 中。敏感值始终在 GitHub Secrets，不写入 Git、镜像、构建缓存、日志或 Actions artifacts。

`main` 的代码推送自动部署新增或有改动的服务。工作流扫描一级目录下的 `deploy/runtime.json`，不维护固定服务名单。选择范围从最近一次成功的 push 工作流计算，包含前次失败或被排队跳过的修改；无法读取基线时直接失败，不扩大到全部服务。

每个服务目录内的变动只选择该服务。公共 `deploy/`、工作流或根文档变化只运行部署器验证，不触发业务容器重建；兼容旧服务的 `deploy/targets.json` 按单个服务条目比较，增加一项不会选择其他服务。需要主动重部署时，手动运行工作流并输入服务名或 `all`。PR 只测试和构建；生产部署只允许 `main`。删除目录不会自动删除生产容器、历史版本或数据。

构建使用 Python 3.12、各项目锁文件和独立 Dockerfile。镜像先用临时测试密钥验证健康、`tools/list` 和错误密钥拒绝，再发布至私有 GHCR。生产拉取使用本次工作流短期 `GITHUB_TOKEN`，按镜像 digest 固定版本，校验镜像的仓库和提交标签。

## 服务器执行与恢复

部署凭据为独立的 `MCP_DEPLOY_SSH_KEY`。服务器只允许此 SSH key 执行 `/usr/local/sbin/mcp-plugins-deploy`；禁止任意 shell、端口转发和 PTY。`MCP_DEPLOY_HOST` 和 `MCP_DEPLOY_KNOWN_HOSTS` 是仓库 Variables，主机公钥经已有管理员 SSH 通道取得，连接时严格校验。

服务器路径：

- `/etc/mcp-plugins/targets.json`：原有 8 个服务的特权基础设施约束；新增普通服务不需要注册到此文件。
- `/opt/mcp-plugins/releases/<服务>/<提交及随机后缀>/`：权限 `0700` 的版本目录；运行配置和密钥文件权限 `0600`。
- `/opt/mcp-plugins/state/<服务>.json`：当前镜像、提交、容器和上一个容器名称，不含密钥。

更新前先核对部署记录的容器 ID、所属服务、端口和挂载，防止接管其他项目容器。首次部署检查名称和端口可用，再创建容器；失败仅移除本次新容器，不写入成功状态，也不删除持久化数据。更新已有服务时，先拉取镜像、校验当前服务和挂载，再停止旧容器并保留为 `<服务>.rollback-<时间戳>`，关闭旧副本的自动重启。原有 8 个服务的新容器保留 host 网络、原有端口和数据挂载；Source MCP 保留 init，Browser Relay 保留浏览器 profile 与 SQLite 状态，浏览器程序使用镜像内版本。

新容器必须通过健康、正确密钥的 MCP 工具发现、错误密钥拒绝和运行环境回读；Browser Relay 还必须通过 `/ready`。验证失败会移除本次新建容器、恢复旧容器名称和重启策略，再验证旧服务。原有镜像、旧容器、数据和历史配置不自动清理。每次替换存在短暂服务中断。

需要手动恢复某次成功部署时，管理员先读取 `state/<服务>.json` 确认 `previous_container`，停止并另行保留当前容器，将记录中的旧容器改回服务名，恢复其 `previous.json` 中的重启策略，启动并执行健康与 MCP 鉴权检查。不要同时启动两个使用相同端口或浏览器 profile 的版本。

原 `/opt/dify-db/docker-compose.yml` 保留为旧部署及配套 Teleport 服务的配置；MCP 已由本工作流维护后，不再用旧 Compose 重建这 8 个服务。`tsh-proxy-*`、`tsh-auth-watchdog`、`sandbox-worker`、Zendesk Agent 的生命周期与凭据消费者不在本工作流内。

## 管理员维护部署器

首次导入使用 `python3 deploy/bootstrap_secrets.py` 预览，`--apply` 写入 GitHub。此脚本以当前运行容器为来源，值只经过 SSH 和内存管道；首次导入后以 GitHub 为管理源，不要用旧服务器文件反向覆盖新值。

首次安装使用 `python3 deploy/install_server.py`。之后更新服务器执行器或白名单使用 `python3 deploy/install_server.py --update-helper-only`，保留 CI SSH key；安装前自动备份旧文件。工作流校验执行器文件哈希，服务器版本与提交不一致时拒绝部署。

环境变量和 Secrets 名称从服务自身配置派生，新增变量无需修改工作流或服务器白名单。旧服务的端口、网络和特权挂载仍受管理员配置约束；变更现有基础设施需要显式迁移。`generate_workflow.py` 只在维护公共工作流时运行，新增服务无需运行。

Secrets 通过 GitHub 的动态索引逐项引用，每个部署任务只接收自己服务声明的值，不展开整个 Secrets 上下文；每个服务最多 32 个环境或文件密钥。命名必须为 `MCP_<服务名大写、横线改下划线>__<环境变量名>`，不允许引用其他服务或部署 SSH 密钥。[GitHub 上下文索引说明](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)。

验证部署代码：`python3 -m unittest discover -s deploy/tests -v`。真实 Docker 集成验证：`python3 deploy/tests/integration_docker.py`，仅使用临时容器、临时数据目录和合成密钥，覆盖首次创建、首次失败清理、已有服务更新、失败回滚及无关容器不变。CI 自动执行这两项。不要把真实密钥用于测试。

## 新增 MCP：只提交服务目录

当前统一构建约定为 Python 3.12 + uv。例如新增 `example-mcp/`，目录中准备完整源码、测试、`Dockerfile`、`pyproject.toml`、`uv.lock`、`requirements.runtime.txt`，以及以下 `deploy/runtime.json`：

```json
{
  "deployment": {
    "port": 18120,
    "auth_key": "EXAMPLE_MCP_API_KEY",
    "health_path": "/health"
  },
  "environment": {
    "EXAMPLE_MCP_HOST": "0.0.0.0",
    "EXAMPLE_MCP_PORT": "18120"
  },
  "smoke_environment": {
    "EXAMPLE_MCP_HOST": "0.0.0.0",
    "EXAMPLE_MCP_PORT": "18120"
  },
  "secrets": {
    "EXAMPLE_MCP_API_KEY": {
      "name": "MCP_EXAMPLE_MCP__EXAMPLE_MCP_API_KEY",
      "required": true
    }
  },
  "secret_files": {}
}
```

端口选择未占用的 1024–65535 端口，不可重复使用其他服务的端口。容器内监听 `0.0.0.0`；新服务自动使用 bridge 网络，仅发布到腾讯云的 `127.0.0.1:<port>`，不直接暴露公网。应用需支持 JSON 健康接口和 `/mcp` 的 `tools/list`；正确 Bearer / X-API-Key 可发现工具，错误密钥返回 401 或 403。`smoke_environment` 只能包含非敏感测试配置，镜像应能在合成认证密钥下启动并列出工具。

在 GitHub Actions Secrets 创建配置中声明的值，然后将新目录推送到 `main`。流水线会自动发现、测试、构建、创建首个容器并验证。不需要编辑 `catalog.py`、`targets.json` 或工作流，也不需要登录服务器注册新服务。外部系统的 API 凭据仍需由你提供，缺少必需 Secret 时会在连接服务器之前失败。

有持久化数据时，可在 `deployment` 增加：

```json
"mounts": [
  {"source": "@data/cache", "destination": "/var/lib/mcp/cache", "readonly": false}
]
```

服务器自动创建 `/opt/mcp-plugins/data/example-mcp/cache`，只供该服务挂载。新服务不接受任意主机目录、Docker socket 或 host 网络。文件密钥通过 `secret_files` 声明 `name`、`required: true` 和 `destination`，路径必须在 `/run/mcp-secrets/` 内。已有服务端口和挂载的变更需要单独迁移，首次接入后普通代码及环境变量变动仍自动按服务部署。
