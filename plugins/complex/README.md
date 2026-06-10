# redshift-mcp-complex

`redshift-mcp` 的业务插件，提供 `query_error_api_by_date` 工具：在 Amazon Redshift
上跑一段固定 SQL，返回 IP 维度的 Error API 命中统计。

本包同时是 **「如何写一个 redshift-mcp 插件」的参考 Demo**。

## 插件契约

1. `pyproject.toml` 声明 entry-point（让主程序自动发现）：

   ```toml
   [project.entry-points."redshift_mcp.plugins"]
   complex = "redshift_mcp_complex:register"
   ```

2. 暴露 `register(ctx: PluginContext) -> None` 入口，在其中用 `ctx.mcp.tool()`
   注册工具，闭包捕获 `ctx` 拿共享资源（连接池 / config / logger / request_id）。

`PluginContext` 由宿主 `redshift_mcp.plugin` 提供，是稳定的公开 API。

## 插件自有配置（约定 + env var）

本插件的查询 SQL **不再硬编码进包**，而是由插件**自带的 `config.yaml`** 提供（结构与 host
`config.yaml` 同构）。host 完全不参与（插件配置内聚原则不变）：插件按以下优先级自行解析配置
（见 `_config.py`），**找不到则报错跳过、不静默兜底**：

1. 环境变量 `REDSHIFT_MCP_COMPLEX_CONFIG` 指向的文件（不重新 build wheel 就想换配置时用）；
2. 默认约定：**插件包目录内的 `config.yaml`**（`<包目录>/config.yaml`）。

配置文件支持 `sql`（内联）或 `sql_file`（相对配置文件目录），二选一：

```yaml
# config.yaml（结构与 host 同构）
sql_file: queries/error_api.sql      # 或：sql: |  SELECT ...
```

仓库里只提交模板 `config.example.yaml` / `queries/error_api.example.sql`；真实
`config.yaml` / `queries/*.sql` 被 `.gitignore` 排除、**不进 git**。

**未配置的行为**：启动时 `complex` 抛错并被 `load_plugins` 隔离跳过（不搞崩 server），日志给出
期望的配置路径与修复指引——按日志里的路径创建 `config.yaml` 或设 env var 即可。

## 构建 / 安装

### 开发（monorepo / uv workspace）

```bash
# 根目录一把 editable 装好主程序 + 所有 workspace 插件
uv sync --all-packages

# dev 跑通本插件：在包目录放一份真实配置（gitignored）
cp src/redshift_mcp_complex/config.example.yaml src/redshift_mcp_complex/config.yaml
cp src/redshift_mcp_complex/queries/error_api.example.sql src/redshift_mcp_complex/queries/error_api.sql
```

### 生产（打包模型：真实配置进 wheel、模板不进）

build wheel 面向生产 —— 先在包目录放好**真实** `config.yaml`（及 `queries/error_api.sql`），
再打包；`pyproject.toml` 的 `[tool.hatch.build]` 用 `artifacts` 把它们强制纳入、用 `exclude`
剔除模板。`uv pip install` 后默认路径即命中，**连 env var 都不必**：

```bash
uv build --package redshift-mcp-complex          # 真实配置随 wheel；模板被排除
uv pip install dist/redshift_mcp_complex-*.whl    # 装进与主程序同一个 venv
```

装好后启动 `redshift-mcp`，`query_error_api_by_date` 工具即出现在 `list_tools` 中。

> 第三方插件同理：自建 wheel、`uv pip install` 进 host 的 venv，靠 entry_points 自动发现加载，
> **不需要改 host 的任何配置**。

## 临时禁用

在主程序 `config.yaml` 里：

```yaml
plugins:
  disabled: ["complex"]
```
