# github-mcp

从腾讯云当前 `github-mcp` 运行容器整理的独立 MCP 项目。端口：`8111`。全部业务代码位于本目录 `src/github_mcp/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest
cp .env.example .env
# 填写本地凭证后启动
uv run --locked github-mcp
```

使用 Python 3.12。

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
