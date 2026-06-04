# redshift-mcp-error-api

`redshift-mcp` 的业务插件，提供 `query_error_api_by_date` 工具：在 Amazon Redshift
上跑一段固定 SQL，返回 IP 维度的 Error API 命中统计。

本包同时是 **「如何写一个 redshift-mcp 插件」的参考 Demo**。

## 插件契约

1. `pyproject.toml` 声明 entry-point（让主程序自动发现）：

   ```toml
   [project.entry-points."redshift_mcp.plugins"]
   error_api = "redshift_mcp_error_api:register"
   ```

2. 暴露 `register(ctx: PluginContext) -> None` 入口，在其中用 `ctx.mcp.tool()`
   注册工具，闭包捕获 `ctx` 拿共享资源（连接池 / config / logger / request_id）。

`PluginContext` 由宿主 `redshift_mcp.plugin` 提供，是稳定的公开 API。

## 构建 / 安装

```bash
# 在 monorepo（uv workspace）根目录，一把装好主程序 + 本插件（editable）
uv sync

# 或单独打成 wheel 后装进与主程序同一个 venv
uv build --package redshift-mcp-error-api
uv pip install dist/redshift_mcp_error_api-*.whl
```

装好后启动 `redshift-mcp`，启动日志会出现 `插件已加载: error_api`，
`query_error_api_by_date` 工具即出现在 `list_tools` 中。

## 临时禁用

在主程序 `config.yaml` 里：

```yaml
plugins:
  disabled: ["error_api"]
```
