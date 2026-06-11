# redshift-mcp

一个独立部署的**通用 Redshift MCP Server + 插件框架**（Streamable HTTP 传输）。核心提供三件套通用查询能力；具体业务查询（如按日期统计 Error API IP 命中）以**可安装插件**形式提供，启动时自动发现并注册。通过 PostgreSQL wire protocol 用 `psycopg3` 连接 **Amazon Redshift**。

## 工具

核心暴露 **3 个通用查询工具**；业务工具通过两种插件机制提供 —— ① 自带的 `redshift-mcp-complex` 这类 **Python 插件**（entry_points 装进同一 venv 即自动发现），② 直接在 `config.yaml` 的 `sql_tools:` 段**声明式注册 SQL 工具**（零代码）。详见下方「[插件系统](#插件系统)」。

### 通用查询三件套（受 `config.yaml` 白名单约束）

- **`list_tables()`** —— 列出 config 白名单中所有允许查询的表（`{name, description}`）。
  LLM 客户端的"发现入口"。
- **`describe_table(table)`** —— `table` 为 `schema.table` 格式且必须在白名单内；
  返回列名、类型、行数估计，叠加 config 中的列说明。
- **`run_sql(sql)`** —— 执行单条 SELECT。**用 [sqlglot](https://github.com/tobymao/sqlglot)
  解析 AST 强制校验**：只允许单条 SELECT、所有引用表 schema-qualified 且在白名单内、
  自动追加 `LIMIT max_rows + 1`。任何 DML / DDL / SET / 多语句一律拒绝。

### 业务插件工具（随仓库自带 `redshift-mcp-complex`）

- **`query_error_api_by_date(date: str)`** —— `date` 为 `YYYY-MM-DD` 格式，返回
  `{date, count, truncated, rows: [{client_ip, device_count}, ...]}`，按 `device_count` 降序。
  由插件包 `redshift-mcp-complex`（`plugins/complex/`）提供，**SQL 由插件自有 `config.yaml`
  提供**（插件按 `env var > 包内约定路径` 自行加载，见 `_config.py`；仓库只提交模板
  `queries/error_api.example.sql`），命名占位符 `%(event_date)s` / `%(limit)s`。不受表白名单约束。
  逻辑示意（实际文件里 `LIKE` 模式的字面 `%` 都写成 `%%` 以避开 psycopg3 占位符扫描）：


## 安装

依赖 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone <你的仓库地址>
cd McpRedshift
uv sync --all-packages    # 主程序 + plugins/* 下所有插件一并 editable 装好
cp config.example.yaml config.yaml
# 编辑 config.yaml：填入 Redshift host/dbname/user/password 与 auth_token
```

## 运行

```bash
uv run redshift-mcp --config config.yaml
# server 监听 http://0.0.0.0:8000/redshift（可配置）

# 免启动列出已安装插件（ep.name / distribution / version）——查 plugins.disabled 该填什么名
uv run redshift-mcp --list-plugins      # 简写 -l
uv run redshift-mcp --version           # 打印版本；--help / -h 看全部参数
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

- **① Python 插件（entry_points）** —— 写代码的全功能插件，独立可安装包，装进同一 venv 即自动发现（**不需要改 host 任何配置**）。**插件私有配置内聚在插件内部**：插件如需外部配置（如自带 `complex` 的业务 SQL），自带一个 `config.yaml`（结构与 host 同构）并自行加载（`env var > 包内约定路径`），host `config.yaml` 不承载插件配置。
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
       async def my_tool(arg: str) -> dict:
           """工具说明（FastMCP 据此 + 类型注解推断 schema）。"""
           # 首选 ctx.aexecute 跑只读查询（复用宿主的执行 / 计时 / 行截断 / 审计），
           # 用 ctx.db_errors 把 DB 异常包成带 rid 的 RuntimeError（不吞编程错误）。
           async with ctx.db_errors(f"{ctx.plugin_name} 查询"):
               return await ctx.aexecute(
                   "SELECT ... WHERE x = %(arg)s",
                   {"arg": arg},
                   max_rows=ctx.config.query.max_rows,
                   source=f"plugin:{ctx.plugin_name}",   # 进完成日志/审计，便于按来源 grep
               )
   ```

`PluginContext`（`redshift_mcp.plugin`）提供 `mcp` / `config` / `logger` / `sql_audit_logger` / `request_id_var` / `get_pool` / `aexecute` / `db_runtime_errors` / `plugin_name`，外加方法 `db_errors(operation="查询", *, logger=None)`，是稳定的公开 API。`aexecute`（= host 的 `db.aexecute`）是插件执行只读查询的**首选入口**；`get_pool` 保留给需要自管连接的特殊场景；`plugin_name` 是本插件的 entry-point 名（用于 `source` / 子 logger，避免硬编码自名）。`db_errors` 的 `operation` 是面向客户端/日志的「什么失败了」标签，默认中性的「查询」即可——**不必放工具名**：客户端错误里的工具名由 FastMCP 的 `Error executing tool <name>:` 前缀自动提供。完整参考实现见 `plugins/complex/`。

### 安装与启用

```bash
# monorepo（uv workspace）开发期：--all-packages 把 plugins/* 下所有插件 editable 装好
uv sync --all-packages

# 或单独打包后装进与主程序同一个 venv（第三方插件即走这条，零 host 改动）
uv build --package redshift-mcp-complex
uv pip install dist/redshift_mcp_complex-*.whl
```

启动日志会打印「插件已加载: complex (...)」。临时禁用某个已安装插件：`config.yaml` 里
`plugins.disabled: ["complex"]`；整体关闭：`plugins.enabled: false`。

### 方式二：声明式 SQL 工具（零代码）

在 `config.yaml` 里声明，启动即注册成 MCP 工具：

```yaml
sql_tools:
  - name: active_devices_by_day
    description: "查询指定日期某国家的活跃设备数"
    sql: |                     # 也可换成 sql_file: queries/active_devices.example.sql
      SELECT country, COUNT(DISTINCT device_id) AS devices
      FROM analytics.events
      WHERE event_date = %(date)s AND country = %(country)s
      GROUP BY country
      LIMIT 100                # LIMIT 可选：缺则自动追加 LIMIT (max_rows+1)；显式写则原样尊重
    params:
      - {name: date, type: date, format: "%Y-%m-%d", description: "US 时区日期"}
      - {name: country, type: enum, enum: ["US", "CA", "UK"], description: "国家码"}
    # max_rows: 5000           # 可选，覆盖全局 query.max_rows
    # safe: true               # 默认开
```

- **参数**：`type` 支持 `string` / `int` / `date` / `enum`；FastMCP 按 schema 校验类型，`date` 额外按 `format` 校验。SQL 用命名占位符 `%(参数名)s` 安全绑定（不拼字符串）；LIKE 模式里字面 `%` 写成 `%%`。
- **安全闸门**：`safe`（默认 `true`）在注册时校验 SQL 是**单条只读 SELECT**（拒 DML/DDL/多语句/SELECT INTO），不通过则跳过该工具并记 error。个别确需特殊语句的可设 `safe: false`（运维自负）。
- **LIMIT 自动下推**：`safe=true` 时若 SQL 顶层无 LIMIT，自动追加 `LIMIT (max_rows+1)`（`max_rows` 取 `spec.max_rows` 或全局 `query.max_rows`）下推到 DB，避免大表全量拉回内存；显式写了 LIMIT 则原样尊重、不收紧。`safe=false` 不自动加，须自带。

### 配置拆分（条目多时）

`sql_tools` / `tables` 多了以后，可拆到独立片段文件，主 `config.yaml` 只留一行 `include`：

```yaml
include:
  - conf.d/*.yaml     # glob，相对主配置目录
```

片段按「列表追加 / 嵌套 dict 深合并 / 标量片段覆盖」并入主配置；片段里的 `sql_tools` 条目可用 `sql_file: ../queries/x.sql`（相对**片段文件**目录）把大 SQL 外链到独立 `.sql`。参考 `conf.d/*.example.yaml` 与 `queries/*.example.sql`。

> **命名约定**：仓库自带的范本 SQL 都用 `*.example.sql` 命名（如 `queries/active_devices.example.sql`、插件包内 `queries/error_api.example.sql`）；`.gitignore` 配套规则把不带 `.example.` 的真实业务 SQL 排除在 git 之外，避免误提交。

## 用 MCP Inspector 调试

```bash
npx @modelcontextprotocol/inspector
```

在 Inspector 界面里：

- Transport: **Streamable HTTP**
- URL: `http://localhost:8000/redshift`
- Headers: 添加 `Authorization` = `Bearer <your-token>`
- 打开 **Tools** 标签页：
  - `query_error_api_by_date` —— `date="2026-05-20"`（由自带 `complex` 插件提供，默认已加载）
  - `list_tables` —— 无参数；返回当前白名单
  - `describe_table` —— `table="analytics.events"`（需在 `tables` 白名单内）
  - `run_sql` —— 例如 `sql="SELECT client_ip FROM analytics.events WHERE event_date='2026-05-20' LIMIT 10"`

## 安全注意事项

- **纵深防御的两层模型（务必理解）**：`run_sql` 的 sqlglot 闸门 + 表白名单是**第一层**，
  其严密程度取决于 sqlglot 的 Redshift 解析保真度；**只读 Redshift 账号是不可省略的第二层**。
  必须用**只读**账号、且权限仅授予所需 schema/表的 SELECT —— 即便闸门被绕过，DB 层也从
  权限上兜底拒绝写操作 / 越权读取。**切勿把闸门当作唯一访问控制。**
- 定期轮换 `auth_token`；把 `config.yaml` 当作 secret 处理（它已经在 `.gitignore`）。
- 所有外部入参（含 `date` / 声明式 SQL 工具的参数）都以 psycopg3 的**命名占位符 `%(name)s`** 绑定，不做字符串拼接 —— 不存在 SQL 注入。`date` 类参数额外用 `datetime.strptime(..., "%Y-%m-%d")` 在工具体里做格式校验。
- `run_sql` 用 sqlglot 解析 AST 强制：单条 SELECT / 全限定表名 / 所有引用表必须在白名单。
  引用与白名单统一归一成三段式 `database.schema.table` 比对（未写库前缀用配置默认库补全），
  挡住三段式 `otherdb.schema.table` 的跨库越权；CTE 别名识别为 in-query 局部命名空间，不当作"未授权表"误杀。
- **声明式 SQL 工具**（`sql_tools:`）默认走只读安全闸门（`safe: true`），注册期校验单条只读 SELECT，
  拦 DML/DDL/多语句/`SELECT INTO`；个别工具如需特殊语句可设 `safe: false`，由运维自负。
  对 `safe: true` 工具，顶层无 `LIMIT` 时自动追加 `LIMIT (max_rows+1)` 下推到 DB、显式写了则原样尊重；`safe: false` 不自动加，须自带。
- **SQL 文本默认不进运行日志**（`sql_audit_level=WARNING`）—— LLM 写 `WHERE email='...'` 之类的 PII 不会落到 `redshift-mcp.log`。
  需要审计时把 `sql_audit_level` 改成 `INFO`；想物理隔离审计与运行日志，再配 `sql_audit_file: ./logs/sql-audit.log` 走独立文件。
- 部署在反向代理之后时，TLS 在上游终结。

## 生产部署

见 [DEPLOY.md](DEPLOY.md)。
