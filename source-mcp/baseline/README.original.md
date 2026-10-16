# source-mcp

Remote MCP service for querying Dify SaaS source repositories.

The default source ref is `runtime`, which means "the code currently running in
production". Runtime refs are resolved from SigNoz deployment telemetry or
explicit `SOURCE_MCP_RUNTIME_REF_OVERRIDES`.

`SOURCE_MCP_RUNTIME_DEPLOYMENTS` accepts one deployment name or a comma-separated
ordered alias list per repository. Source MCP uses the first alias with a current
image tag. If telemetry contains a tag that Git has not published, the tool
returns `GIT_REF_UNPUBLISHED` instead of conflating it with missing runtime
telemetry. SigNoz transport or response failures return
`RUNTIME_TELEMETRY_FAILED` and do not fall through to older deployment aliases.

Tools:

- `source_runtime_ref`
- `source_search`
- `source_read`
- `source_log`
- `source_diff`
- `repo_status`
- `refresh_repos`
- `list_repo_tags`

`GET /health` reports container zombie count plus cgroup PID usage. It marks
`ok=false` when zombies reach `SOURCE_MCP_PROCESS_ZOMBIE_WARNING_THRESHOLD` or
PID usage reaches `SOURCE_MCP_PROCESS_PIDS_WARNING_RATIO`. It also reports the
active `full_repo_search_concurrency` setting.

Full-repository searches are bounded by
`SOURCE_MCP_FULL_REPO_SEARCH_CONCURRENCY`. The safe default is `1`; accepted
values are `1` through `4`. A restart is required after changing the setting.

Run locally:

```bash
uv sync --extra dev
uv run source-mcp
```
