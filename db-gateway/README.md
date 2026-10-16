# db-gateway

从腾讯云当前 `db-gateway` 运行容器整理的独立 MCP 项目。端口：`18081`。全部业务代码位于本目录 `src/db_gateway/`，不依赖兄弟目录。

## 开发和测试

```bash
uv sync --locked --group dev
uv run --locked --group dev python -m pytest
cp .env.example .env
# 填写本地凭证后启动
uv run --locked db-gateway
```

使用 Python 3.12。需要 PostgreSQL 的 `psql` 和与环境匹配的 Teleport `tsh`；`tsh` 需在运行时安装或挂载。复制 `config.example.yaml` 为 `config.yaml` 后配置数据库代理。

## 构建

```bash
docker build -t db-gateway:local .
```

Dockerfile 使用本目录完整源码和容器实际依赖版本，不依赖服务器上的旧镜像标签。生产配置、API 密钥、浏览器资料和业务数据应单独挂载，不能放入构建上下文。

## 版本来源

- 原镜像：`db-gateway:latest`。
- 镜像 ID：`sha256:378972148a4081b135ca2fdf3870bd36dbc04940fc38a8c185048efd4465887e`。
- `baseline/runtime.json` 记录实际依赖与容器来源；`baseline/source-manifest.json` 记录原始文件哈希。
- `requirements.runtime.txt` 固定运行依赖；`pyproject.toml` 通过 constraints 保持同样版本。原锁文件和部署文本保存在 `baseline/`，不作为当前部署入口。
- 原功能说明见 [baseline/README.original.md](baseline/README.original.md)。
