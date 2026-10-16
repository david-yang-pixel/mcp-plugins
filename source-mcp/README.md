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
