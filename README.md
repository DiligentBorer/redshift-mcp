# redshift-mcp

一个独立部署的**通用 Redshift MCP Server + 插件框架**（Streamable HTTP 传输）。核心提供三件套通用查询能力；具体业务查询（如按日期统计 Error API IP 命中）以**可安装插件**形式提供，启动时自动发现并注册。通过 PostgreSQL wire protocol 用 `psycopg3` 连接 **Amazon Redshift**。

## 工具

核心暴露 **3 个通用查询工具**；业务工具通过两种插件机制提供 —— ① 自带的 `redshift-mcp-error-api` 这类 **Python 插件**（entry_points 装进同一 venv 即自动发现），② 直接在 `config.yaml` 的 `sql_tools:` 段**声明式注册 SQL 工具**（零代码）。详见下方「[插件系统](#插件系统)」。

### 通用查询三件套（受 `config.yaml` 白名单约束）

- **`list_tables()`** —— 列出 config 白名单中所有允许查询的表（`{name, description}`）。
  LLM 客户端的"发现入口"。
- **`describe_table(table)`** —— `table` 为 `schema.table` 格式且必须在白名单内；
  返回列名、类型、行数估计，叠加 config 中的列说明。
- **`run_sql(sql)`** —— 执行单条 SELECT。**用 [sqlglot](https://github.com/tobymao/sqlglot)
  解析 AST 强制校验**：只允许单条 SELECT、所有引用表 schema-qualified 且在白名单内、
  自动追加 `LIMIT max_rows + 1`。任何 DML / DDL / SET / 多语句一律拒绝。

### 业务插件工具（随仓库自带 `redshift-mcp-error-api`）

- **`query_error_api_by_date(date: str)`** —— `date` 为 `YYYY-MM-DD` 格式，返回
  `{date, count, truncated, rows: [{client_ip, device_count}, ...]}`，按 `device_count` 降序。
  由插件包 `redshift-mcp-error-api`（`plugins/error_api/`）提供，**SQL 内聚在插件包内**
  `plugins/error_api/src/redshift_mcp_error_api/queries/error_api.sql`（`query.py` 通过
  `importlib.resources` 读取），命名占位符 `%(event_date)s` / `%(limit)s`。不受表白名单约束。
  逻辑示意（实际文件里 `LIKE` 模式的字面 `%` 都写成 `%%` 以避开 psycopg3 占位符扫描）：


## 安装

依赖 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
cd McpRedshift
uv sync
cp config.example.yaml config.yaml
# 编辑 config.yaml：填入 Redshift host/dbname/user/password 与 auth_token
```

## 运行

```bash
uv run redshift-mcp --config config.yaml
# server 监听 http://0.0.0.0:8000/redshift（可配置）
```

所有请求必须带：

```
Authorization: Bearer <server.auth_token>
```

缺失或错误的 token → `401 Unauthorized`。

## 配置（`config.yaml`）

参见 [`config.example.yaml`](config.example.yaml)。关键字段：

| Section | Key | 用途 |
| --- | --- | --- |
| `database` | `host`, `port`, `dbname`, `user`, `password` | Redshift 连接信息 |
| `database` | `sslmode` | Redshift 推荐设为 `require` |
| `database` | `pool_min_size`, `pool_max_size` | 连接池大小 |
| `server` | `host`, `port`, `path` | HTTP 监听地址 + 端点路径 |
| `server` | `auth_token` | 静态 Bearer Token |
| `query` | `statement_timeout_ms` | 单次 SQL 超时（毫秒） |
| `query` | `max_rows` | 结果行数上限 |
| `plugins` | `enabled` | 是否加载插件（默认 `true`；`false` 整体跳过）|
| `plugins` | `disabled` | 已安装但临时不启用的插件名列表（按 entry-point name）|
| `sql_tools` | 列表 | 声明式 SQL 工具（零代码，见「插件系统」）；每项 `{name, description, sql/sql_file, params, max_rows?, safe?}` |
| `include` | 列表 | 顶层 glob，把片段文件合并进主配置（见「配置拆分」）|
| `logging` | `level` | 主日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）|
| `logging` | `sql_audit_level` | **SQL 审计独立开关**（默认 `WARNING` = 不输出 SQL 文本，PII 安全）。改 `INFO` 即可看每条 `run_sql` 的完整 SQL；与 `level` 正交，**不会**放出 uvicorn / mcp 的内部 DEBUG |
| `logging` | `sql_audit_file` | SQL 审计独立文件路径。`null`（默认）= 与运行日志合流；非空 = 独立写到该文件，便于做单独 retention / 加密 / SIEM 接入 |
| `tables` | 列表 | 通用查询能力的表白名单（schema.table）。留空 `[]` 等于禁用三件套 |

可以用 `REDSHIFT_MCP_CONFIG=/path/to/config.yaml` 覆盖配置文件路径。

## 插件系统

核心包 `src/redshift_mcp/` 不含任何业务 SQL。扩展工具有**两种机制**：

- **① Python 插件（entry_points）** —— 写代码的全功能插件，独立可安装包，装进同一 venv 即自动发现。**插件私有配置内聚在插件内部**（如自带 `error_api` 的 SQL 在其包内 `queries/error_api.sql`），host `config.yaml` 不承载插件配置。
- **② 声明式 SQL 工具（零代码）** —— 直接在 `config.yaml` 的 `sql_tools:` 里声明 `{name, description, sql, params}`，启动自动注册成 MCP 工具。适合简单 SQL。

### 方式一：写一个 Python 插件

1. 建一个独立包，在 `pyproject.toml` 声明 entry-point（group 固定为 `redshift_mcp.plugins`）：

   ```toml
   [project.entry-points."redshift_mcp.plugins"]
   my_plugin = "my_plugin_pkg:register"
   ```

2. 暴露 `register(ctx: PluginContext) -> None`，在其中用 `ctx.mcp.tool()` 注册工具：

   ```python
   from redshift_mcp.plugin import PluginContext

   def register(ctx: PluginContext) -> None:
       @ctx.mcp.tool()
       def my_tool(arg: str) -> dict:
           """工具说明（FastMCP 据此 + 类型注解推断 schema）。"""
           pool = ctx.get_pool()           # 复用宿主的同一个连接池
           rid = ctx.request_id_var.get()  # 每请求关联 id
           ...
   ```

`PluginContext`（`redshift_mcp.plugin`）提供 `mcp` / `config` / `logger` / `sql_audit_logger` / `request_id_var` / `get_pool` / `db_runtime_errors`，是稳定的公开 API。完整参考实现见 `plugins/error_api/`。

### 安装与启用

```bash
# monorepo（uv workspace）开发期：uv sync 一并把自带的 error_api editable 装好
uv sync

# 或单独打包后装进与主程序同一个 venv
uv build --package redshift-mcp-error-api
uv pip install dist/redshift_mcp_error_api-*.whl
```

启动日志会打印「插件已加载: error_api (...)」。临时禁用某个已安装插件：`config.yaml` 里
`plugins.disabled: ["error_api"]`；整体关闭：`plugins.enabled: false`。

### 方式二：声明式 SQL 工具（零代码）

在 `config.yaml` 里声明，启动即注册成 MCP 工具：

```yaml
sql_tools:
  - name: active_devices_by_day
    description: "查询指定日期某国家的活跃设备数"
    sql: |                     # 也可换成 sql_file: queries/active_devices.sql
      SELECT country, COUNT(DISTINCT device_id) AS devices
      FROM analytics.events
      WHERE event_date = %(date)s AND country = %(country)s
      GROUP BY country
      LIMIT 100                # 务必自带 LIMIT（安全闸门不会自动加）
    params:
      - {name: date, type: date, format: "%Y-%m-%d", description: "US 时区日期"}
      - {name: country, type: enum, enum: ["US", "CA", "UK"], description: "国家码"}
    # max_rows: 5000           # 可选，覆盖全局 query.max_rows
    # safe: true               # 默认开
```

- **参数**：`type` 支持 `string` / `int` / `date` / `enum`；FastMCP 按 schema 校验类型，`date` 额外按 `format` 校验。SQL 用命名占位符 `%(参数名)s` 安全绑定（不拼字符串）；LIKE 模式里字面 `%` 写成 `%%`。
- **安全闸门**：`safe`（默认 `true`）在注册时校验 SQL 是**单条只读 SELECT**（拒 DML/DDL/多语句/SELECT INTO），不通过则跳过该工具并记 error。个别确需特殊语句的可设 `safe: false`（运维自负）。注意闸门**不自动加 LIMIT**，请自带。

### 配置拆分（条目多时）

`sql_tools` / `tables` 多了以后，可拆到独立片段文件，主 `config.yaml` 只留一行 `include`：

```yaml
include:
  - conf.d/*.yaml     # glob，相对主配置目录
```

片段按「列表追加 / 嵌套 dict 深合并 / 标量片段覆盖」并入主配置；片段里的 `sql_tools` 条目可用 `sql_file: ../queries/x.sql`（相对**片段文件**目录）把大 SQL 外链到独立 `.sql`。参考 `conf.d/*.example.yaml` 与 `queries/*.sql`。

## 用 MCP Inspector 调试

```bash
npx @modelcontextprotocol/inspector
```

在 Inspector 界面里：

- Transport: **Streamable HTTP**
- URL: `http://localhost:8000/redshift`
- Headers: 添加 `Authorization` = `Bearer <your-token>`
- 打开 **Tools** 标签页：
  - `query_error_api_by_date` —— `date="2026-05-20"`（由自带 `error_api` 插件提供，默认已加载）
  - `list_tables` —— 无参数；返回当前白名单
  - `describe_table` —— `table="analytics.events"`（需在 `tables` 白名单内）
  - `run_sql` —— 例如 `sql="SELECT client_ip FROM analytics.events WHERE event_date='2026-05-20' LIMIT 10"`

## 安全注意事项

- 使用**只读** Redshift 账号 —— 即便 `run_sql` 校验出 bug，DB 层也兜底拒绝写操作。
- 定期轮换 `auth_token`；把 `config.yaml` 当作 secret 处理（它已经在 `.gitignore`）。
- 所有外部入参（含 `date` / 声明式 SQL 工具的参数）都以 psycopg3 的**命名占位符 `%(name)s`** 绑定，不做字符串拼接 —— 不存在 SQL 注入。`date` 类参数额外用 `datetime.strptime(..., "%Y-%m-%d")` 在工具体里做格式校验。
- `run_sql` 用 sqlglot 解析 AST 强制：单条 SELECT / 全限定表名 / 所有引用表必须在白名单。
  CTE 别名识别为 in-query 局部命名空间，不当作"未授权表"误杀。
- **声明式 SQL 工具**（`sql_tools:`）默认走只读安全闸门（`safe: true`），注册期校验单条只读 SELECT，
  拦 DML/DDL/多语句/`SELECT INTO`；个别工具如需特殊语句可设 `safe: false`，由运维自负。
  闸门**不自动加 `LIMIT`**，运维应在 SQL 里自带。
- **SQL 文本默认不进运行日志**（`sql_audit_level=WARNING`）—— LLM 写 `WHERE email='...'` 之类的 PII 不会落到 `redshift-mcp.log`。
  需要审计时把 `sql_audit_level` 改成 `INFO`；想物理隔离审计与运行日志，再配 `sql_audit_file: ./logs/sql-audit.log` 走独立文件。
- 部署在反向代理之后时，TLS 在上游终结。

## 生产部署

部署到一台干净的 RHEL 系服务器（systemd + nginx + TLS），见 [DEPLOY.md](DEPLOY.md)。
