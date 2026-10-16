# MCP 服务仓库

每个一级项目目录对应一个独立 MCP 服务，包含该服务全部业务源码、测试、配置示例、依赖锁文件、Dockerfile 和版本来源记录。基线来自 2026-10-16 核对的腾讯云运行容器。

```text
mcp-plugins/
├── db-gateway/
├── signoz-mcp/
├── source-mcp/
├── github-mcp/
├── docs-tool-service/
├── notification-service/
├── zendesk-mcp/
└── zendesk-browser-relay/
```

GitHub、文档、通知原先共用 tool_services 包，这里按各自线上版本拆为三个完整项目，不依赖共享目录或兄弟项目。sandbox-worker 不在本仓库维护范围。

## 使用

进入对应项目目录，按该项目 README 执行 `uv sync --locked --extra dev`、测试和启动。DB Gateway 使用 `--group dev`。统一使用 Python 3.12；Docker 镜像从各自目录构建。

每个目录包含：

- `src/`、`tests/`：该 MCP 的完整应用及测试。
- `pyproject.toml`、`uv.lock`：独立的项目定义与依赖锁定。
- `requirements.runtime.txt`：线上实际运行依赖的固定版本。
- `.env.example`、`Dockerfile`：配置示例与独立构建入口。
- `baseline/`：容器来源、原始源码哈希、原始锁文件和历史部署材料；历史材料不作为当前部署入口。

## 基线与边界

5 个服务的应用源码逐字保留容器版本。GitHub、文档、通知仅拆分包名、服务注册、启动入口和配置字段；业务工具、认证辅助和 MCP 传输函数保留各自线上实现。原服务器已有的两个 SSE 测试及一个 Zendesk 标签更新测试已调整为验证线上现有行为。

运行依赖按各自容器版本固定，原先锁文件的漂移已在当前 uv.lock 中对齐。Source MCP 和 Browser Relay 补齐了代码实际导入的 httpx 运行依赖。测试额外依赖单独放在开发依赖中。

本仓库不包含生产 .env、密钥、Teleport 凭证、浏览器登录资料或业务数据。DB Gateway 的 tsh/psql、Browser Relay 的浏览器及其他外部服务仍需按项目说明配置。

原三方核对材料见 [.baseline/audit/report.md](.baseline/audit/report.md)。此目录为归档依据；后续整理结果以各项目 README、baseline 和当前锁文件为准。

## 本次验证

初始基线的 8 个项目通过独立锁定安装和 wheel 构建，共 270 项测试；源码一致性记录见 [.baseline/source-integrity.json](.baseline/source-integrity.json)。该记录描述整理代码时的验证，后续 CI/CD 结果以 GitHub Actions 和服务器部署状态为准。

## 密钥与 CI/CD

8 个 MCP 的密钥在 GitHub Actions Secrets 中按服务前缀管理，普通配置在各服务 `deploy/runtime.json`。新增服务通过自身 `deploy/runtime.json` 自动发现并首次部署，无需修改中央名单或工作流。推送 `main` 只测试、构建并部署新增或有改动的服务，注册新服务不会重部署其他 MCP；仅更新密钥时，在 Actions 选择服务并运行部署工作流。

新增 MCP 只需声明容器内监听端口，首次部署自动分配并保存空闲主机端口，生成 `https://zendesk.smlershou.top/<服务名>/mcp` 的 Nginx 路由，并验证公开 HTTPS 工具发现与鉴权。更新复用原端口，只修改对应 MCP；既有服务路由与别名保持兼容。

部署保留数据挂载和旧容器，服务器检查失败恢复对应容器与路由。新增和修改规范见 [AGENTS.md](AGENTS.md)，密钥清单、操作步骤、服务器路径及恢复方法见 [部署说明](deploy/README.md)。
