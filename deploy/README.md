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

修改 Secrets 不会触发 GitHub 的 push 事件。更新完成后进入 **Actions → MCP CI and production deployment → Run workflow**，选择 `main` 和对应服务，或选择 `all`。工作流自动测试、构建并部署，不需要再登录服务器手工修改 `.env`。同一把密钥在其他服务或外部调用方也被使用时，应协调更新这些使用方；本仓库不会修改 Zendesk Agent 或其他仓库的配置。

## 代码与普通配置

非敏感运行配置保存在各服务的 `deploy/runtime.json`，DB 的原生产 YAML 以等价 JSON 存在 `settings` 中。敏感值始终在 GitHub Secrets，不写入 Git、镜像、构建缓存、日志或 Actions artifacts。

`main` 的代码推送自动部署受影响服务。选择范围从最近一次成功的 push 工作流计算，包含前次失败或被排队跳过的修改。公共部署代码变化会验证全部 8 个服务。PR 只测试和构建；生产部署只允许 `main`。

构建使用 Python 3.12、各项目锁文件和独立 Dockerfile。镜像先用临时测试密钥验证健康、`tools/list` 和错误密钥拒绝，再发布至私有 GHCR。生产拉取使用本次工作流短期 `GITHUB_TOKEN`，按镜像 digest 固定版本，校验镜像的仓库和提交标签。

## 服务器执行与恢复

部署凭据为独立的 `MCP_DEPLOY_SSH_KEY`。服务器只允许此 SSH key 执行 `/usr/local/sbin/mcp-plugins-deploy`；禁止任意 shell、端口转发和 PTY。`MCP_DEPLOY_HOST` 和 `MCP_DEPLOY_KNOWN_HOSTS` 是仓库 Variables，主机公钥经已有管理员 SSH 通道取得，连接时严格校验。

服务器路径：

- `/etc/mcp-plugins/targets.json`：管理员维护的服务、端口、挂载、环境变量白名单。
- `/opt/mcp-plugins/releases/<服务>/<提交及随机后缀>/`：权限 `0700` 的版本目录；运行配置和密钥文件权限 `0600`。
- `/opt/mcp-plugins/state/<服务>.json`：当前镜像、提交、容器和上一个容器名称，不含密钥。

先拉取镜像、校验当前服务和挂载，再停止旧容器并保留为 `<服务>.rollback-<时间戳>`，关闭旧副本的自动重启。新容器保留 host 网络、原有端口和数据挂载；Source MCP 保留 init，Browser Relay 保留浏览器 profile 与 SQLite 状态，浏览器程序使用镜像内版本。

新容器必须通过健康、正确密钥的 MCP 工具发现、错误密钥拒绝和运行环境回读；Browser Relay 还必须通过 `/ready`。验证失败会移除本次新建容器、恢复旧容器名称和重启策略，再验证旧服务。原有镜像、旧容器、数据和历史配置不自动清理。每次替换存在短暂服务中断。

需要手动恢复某次成功部署时，管理员先读取 `state/<服务>.json` 确认 `previous_container`，停止并另行保留当前容器，将记录中的旧容器改回服务名，恢复其 `previous.json` 中的重启策略，启动并执行健康与 MCP 鉴权检查。不要同时启动两个使用相同端口或浏览器 profile 的版本。

原 `/opt/dify-db/docker-compose.yml` 保留为旧部署及配套 Teleport 服务的配置；MCP 已由本工作流维护后，不再用旧 Compose 重建这 8 个服务。`tsh-proxy-*`、`tsh-auth-watchdog`、`sandbox-worker`、Zendesk Agent 的生命周期与凭据消费者不在本工作流内。

## 管理员维护部署器

首次导入使用 `python3 deploy/bootstrap_secrets.py` 预览，`--apply` 写入 GitHub。此脚本以当前运行容器为来源，值只经过 SSH 和内存管道；首次导入后以 GitHub 为管理源，不要用旧服务器文件反向覆盖新值。

首次安装使用 `python3 deploy/install_server.py`。之后更新服务器执行器或白名单使用 `python3 deploy/install_server.py --update-helper-only`，保留 CI SSH key；安装前自动备份旧文件。工作流校验执行器文件哈希，服务器版本与提交不一致时拒绝部署。

新增环境变量时，同时更新服务映射和 `deploy/targets.json`，运行 `python3 deploy/generate_workflow.py` 更新显式 Secret 引用，再通过管理员通道更新服务器白名单。部署执行器不会接受任意新增环境变量或挂载路径。

验证部署代码：`python3 -m unittest discover -s deploy/tests -v`。不要把真实密钥用于测试。
