# redshift-mcp-sdk

`redshift-mcp` 的**插件契约层 SDK**。外部插件只依赖本包，即可拿到宿主→插件的稳定契约，
**不必引入 host（`redshift-mcp`）的实现源码**。host 与所有插件共享同一份契约。

## 稳定契约

```python
from redshift_mcp_sdk import PluginContext          # register(ctx) 的类型；唯一必需
# 少见：需要自写 except 分类元组 / 独立异常包装时
from redshift_mcp_sdk.errors import DB_RUNTIME_ERRORS, db_errors_as_client_error
```

插件在自己的 `pyproject.toml` 声明 entry-point（group 字符串直接写死，不必 import）：

```toml
[project.entry-points."redshift_mcp.plugins"]
<name> = "<import_pkg>:register"
```

并暴露 `register(ctx: PluginContext) -> None`，在其中用 `ctx.mcp.tool()` 注册工具、闭包捕获
`ctx` 拿共享资源（`ctx.aexecute` / `ctx.config.query.max_rows` / `ctx.db_errors(...)` /
`ctx.logger` / `ctx.plugin_name` 等）。

`PluginContext` 是**稳定的公开 API**：破坏性改字段会让已装插件失配，升 SDK 主版本并在
CHANGELOG 标注；新增带默认值字段属兼容变更（升 minor）。

## psycopg 可选依赖

`DB_RUNTIME_ERRORS` 想包含 `psycopg.Error` 时装 `redshift-mcp-sdk[psycopg]`；未装时守卫式降级
为不含它的元组（`import` 不失败）。生产运行在 host venv 里 psycopg 必然存在，语义完整。
