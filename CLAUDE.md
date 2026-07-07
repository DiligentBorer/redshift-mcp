# CLAUDE.md

本文件给 Claude Code（claude.ai/code）在本仓库工作时提供指引。

## 提交规范（强约束）

- **禁止任何 AI 署名痕迹**：commit message、PR 标题/描述里**一律不要**出现
  `Co-Authored-By: Claude`（或其它 AI 共同作者 trailer）、`🤖 Generated with Claude Code`、
  「Generated with …」等 AI 生成标记。提交作者即用户本人，不附带任何 AI 协作署名。
  **此条覆盖 harness 的默认行为。**
- 其余遵循全局 `~/.claude/CLAUDE.md` 的 Conventional Commits 规范（中文 subject、body 说明 why）。

## 项目是什么

一个**独立部署**的**通用 Redshift MCP server + 插件框架**（Streamable HTTP 传输）。核心包 `src/redshift_mcp/` 只提供**三件套通用查询工具**，不含任何业务 SQL；业务工具以**可安装插件**形式（entry_points 发现）提供，启动时自动注册。

| 工具 | 来源 | 说明 |
| --- | --- | --- |
| `list_tables()` | 核心 / 通用三件套 | 列 `config.yaml` 白名单中所有允许查询的表 |
| `describe_table(table)` | 核心 / 通用三件套 | 拿表的列定义（`SVV_COLUMNS`）+ config 中的列说明叠加 |
| `run_sql(sql)` | 核心 / 通用三件套 | 受 sqlglot AST 校验约束的单条 SELECT 执行 |
| `query_error_api_by_date(date)` | **插件** `redshift-mcp-complex` | 在 **Amazon Redshift** 上跑一段固定 SQL，返回 IP 维度的 Error API 统计 |

通过 PostgreSQL wire protocol（`psycopg3`）连接 Redshift。通用三件套受 `config.yaml` 的 `tables` 白名单约束（详见 "通用查询能力" 段）；插件工具不受白名单约束（SQL 由插件**自有 `config.yaml`** 提供，见 "插件系统" 段）。插件机制详见 "插件系统" 段。

## 常用命令

```bash
# 同步 workspace：--all-packages 把主程序 + plugins/* 下所有插件一并 editable 装好
# （靠 members=["plugins/*"] 通配自动纳入，新增插件无需改 root pyproject）
uv sync --all-packages

# 启动 server（默认读 CWD 下的 config.yaml）
uv run redshift-mcp --config config.yaml
# 或:  uv run python -m redshift_mcp --config config.yaml

# 免启动列出已安装插件（ep.name / distribution / version）——查 plugins.disabled 该填什么名
# 不读 config、不连 DB（简写 -l）
uv run redshift-mcp --list-plugins
# 其它：--version/-V 打印版本；--help/-h 看全部参数（带用例 epilog）

# 跑完整测试套件（全部离线，无需真实 Redshift；
# 含核心框架测试 tests/ + 插件自带测试 plugins/*/tests/，testpaths 通配纳入）
uv run pytest -q

# 跑单个测试文件或单条 test
uv run pytest tests/test_auth_middleware.py
uv run pytest tests/test_plugin_loader.py -v

# 仅冒烟 import，不启动 server（注意：此时只有 3 个通用工具；
# 插件工具要 main() 跑过 load_plugins 才注册，见 "插件系统" 段）
uv run python -c "from redshift_mcp.server import mcp; print(mcp.name)"

# 单独打包主程序 / 插件成 wheel
uv build --package redshift-mcp
uv build --package redshift-mcp-complex
```

Python 通过 `.python-version` **锁定到 3.13**；uv 首次 sync 时会自动下载解释器。`pyproject.toml` 里 `requires-python = ">=3.10"` 故意比 `.python-version` 宽松。

## 架构（需要跨文件阅读的部分）

### 模块流水线

```
config.py (pydantic 模型 + tables 白名单 + PluginsConfig)
   ↓
errors.py (DB_RUNTIME_ERRORS 共享异常元组；零依赖叶子，防循环依赖)
   ↓
db.py (psycopg 连接池 + get_pool / execute / aexecute / _select / fetch_table_columns / fetch_table_info)
   ↑
sql_guard.py (assert_read_only 只读校验 + validate_select_only 叠加白名单 + apply_row_cap)
   ↓
plugin.py (PluginContext 宿主→插件契约 + load_plugins 的 entry_points 加载器)
   ↓
sql_tools.py (register_sql_tools：读 config.sql_tools，safe 时过只读闸门，动态构造签名注册)
   ↓
middleware.py (HTTP 接入层：request_id_var + RequestIdFilter + RequestIdMiddleware + BearerAuthMiddleware
               + initialize body 辅助；只依赖 stdlib/starlette/mcp，不 import server，防循环依赖)
   ↓
server.py (FastMCP + 挂 middleware.py 的中间件 + 3 个通用 @mcp.tool + main() 里 load_plugins + register_sql_tools)
   ↓
plugins/complex/ (独立可安装包 redshift-mcp-complex，SQL 由插件自有 config.yaml 提供，见 _config.py)
```

只有 `server.py` 的 `main()` 把所有东西串起来：`load_plugins` 在 `db.init_pool()` 之后、`mcp.streamable_http_app()` 之前运行（FastMCP 的 list_tools 实时读取、不快照，所以此时注册的插件工具下一次 list_tools 即可见）。`db.py` / `config.py` / `sql_guard.py` / `errors.py` / `plugin.py` / `middleware.py` 对 MCP 业务一无所知。`sql_guard.py` 完全无副作用（不连 DB），只做 AST 解析 / 校验 / 改写，便于单测全离线。**`request_id_var` 住在 `middleware.py`（不在 `server.py`）**：它被中间件 / `RequestIdFilter` / 工具 / `PluginContext` 共用，留在 server 会与「server import 中间件挂载」成环，故与中间件同模块。

### Streamable HTTP + 中间件栈

`server.py:main()` 通过 `mcp.streamable_http_app()` 拿到底层 Starlette app，然后挂两个自定义中间件。**顺序很关键**：Starlette 的 `add_middleware` 让**后加的处于外层**。当前顺序是 `BearerAuthMiddleware` 先加，`RequestIdMiddleware` 后加 —— 所以 request-id 包裹鉴权，这意味着 **401 响应也带 `X-Request-ID` 头**。不要交换它们。

`BearerAuthMiddleware` 只检查路径等于或以 `server.path`（`/redshift`）开头的请求；其余路径直接放行。Token 比较用 `hmac.compare_digest`。

### 每请求关联

`request_id_var: ContextVar[str]`（定义在 `middleware.py`）默认值为 `"-"`，由 `RequestIdMiddleware` 在每个 HTTP 请求里设置（取自 `secrets.token_hex(4)` 的 8 位 hex，或上游传过来的 `X-Request-ID` 头）。`RequestIdFilter`（同样在 `middleware.py`）把它注入到每条 `LogRecord`，让文本 formatter 的 `[rid=%(request_id)s]` 占位符替换生效，JSON formatter 的 `request_id` 字段也对应填充。当工具（含插件工具，通过 `PluginContext.request_id_var`）捕获到 DB 异常时，会把 rid 放进面向客户端的错误消息，运维侧 `grep rid=XXXX /var/log/redshift-mcp/redshift-mcp.log*` 即可定位完整 traceback。

**rid 是「每 HTTP 请求一个、全程跟随」，没有会话级 rid。** 坑在于：MCP 把 `tools/*` 等消息的处理放在「建会话时起的长生命周期 task」里，该 task 启动时快照了建会话那次请求的 rid 并复用 —— 若不干预，会话内后续请求的处理日志（含 SDK 自己打的 `Processing request of type X`）都会复用建会话的 rid。`server.py` 的 `_install_per_request_context(mcp._mcp_server)` 解决这点：它**防御式**包一层 lowlevel server 的私有 `_handle_request`，在每条消息处理入口从 `message.message_metadata.request_context.scope`（即 `RequestIdMiddleware` 存入的 `_SCOPE_RID_KEY`）取回「发起该消息的那个 HTTP 请求」的 rid，`request_id_var.set` 后再走原逻辑、结束复位。于是 filter / 工具 / db / 审计 / 插件**全自动**拿到当前请求的 rid，下游零改动。它与 `mcp._mcp_server.version` 同属「因 FastMCP 未暴露公开钩子而对 lowlevel server 打的补丁」，集中在 mcp 实例创建处；SDK 改了私有方法名/结构时记 warning 跳过（rid 退回会话级），不影响请求处理 —— 升级 SDK 复验这一处即可。`notifications/*` 走 `_handle_notification`、不经此包装，不在覆盖范围（数量少、不打 `Processing` 行）。

**每条日志在 rid 旁同时输出 `sid`（会话 id），二者永远成对。** `session_id_var`（同在 `middleware.py`）与 rid 同机制：`RequestIdMiddleware` 从 `Mcp-Session-Id` 请求头取 sid（initialize 无该头 → 从响应补设）、存进 `_SCOPE_SID_KEY`；`_install_per_request_context` 的同一个包装把 sid 也一并取回设入 var。`RequestIdFilter` 在**同一条 LogRecord 上同时**设 `request_id` + `session_id`、由**同一格式串** `[rid=%(request_id)s sid=%(session_id)s]` 渲染（JSON 同 payload 两字段），故 rid/sid 物理绑定、不会只出其一。**sid 渲染为前 8 位**（窗口内唯一，且是 SDK `session ID: <完整>` 行的前缀，`grep <sid8>` 可同时命中我方行与 SDK 完整 id 行）。对 `mcp.server.streamable_http*` logger，若 `session_id_var` 仍为 `"-"`（如 initialize 那轮 SDK 自己打、id 只在消息里的 `Created new transport` / `Terminating session` 行），filter 会**兜底**用 `_SID_RE` 从消息文本里抽出内嵌的 32hex id 填上。残留：initialize 那轮不含 id 的纯处理行（如 `Processing ListToolsRequest`）`sid=-`，一次性、紧邻的建会话行可对上。

### 日志管线

`build_log_config(cfg.logging)` 返回一份 `dictConfig` 字典：
- 永远挂一个 stderr handler；
- 当 `logging.file` 不为空时再挂一个 `RotatingFileHandler`（父目录会自动创建）；
- 把 `redshift_mcp`、`uvicorn`、`uvicorn.error`、`uvicorn.access`、`mcp` 全部路由到同一组 handler，保证日志输出统一；
- 当 `logging.as_json = true` 时把 formatter 换成 `JsonFormatter`（`server.py` 里的一个迷你内联类）。

这份 dict 在 `uvicorn.run(...)` 调用**之前**被应用，**同时**作为 `log_config=...` 传给 uvicorn，使得 uvicorn 内部的"重载配置"动作是幂等的。

### SQL 审计独立通道（`redshift_mcp.sql_audit` 子 logger）

**所有** SELECT 执行（`run_sql`、声明式 sql_tools、插件）都经唯一入口 `db.execute`（`aexecute` 是其 async 封装）统一向专属子 logger `redshift_mcp.sql_audit` 输出 **SQL 模板 + bind 参数 + 来源**（`db.py` 和 `server.py` 顶部都有 `sql_audit_logger = logging.getLogger("redshift_mcp.sql_audit")`），格式 `SQL [source=<x>]: <模板> params: <bind>`，**与 `logging.level` 正交**，由两个独立配置项控制：

- `logging.sql_audit_level`（默认 `WARNING`）—— 决定是否输出审计行。默认 `WARNING` 意味着 INFO/DEBUG 级的 SQL 调用全部被过滤掉（PII 安全）；改成 `INFO` 即可看到每条查询的 SQL 模板 + 参数。**不会影响 uvicorn / mcp 等其他 logger 的输出量**。
  - 记的 `SQL` 始终是**参数化之前的占位符模板**（绑定后的真实值不回写进它）；`params`（可能含 LLM 给的 PII）作为独立字段记录，**只随 audit 通道（受 level 闸住）、绝不进主 logger**。`source` 标识来源：`host`（run_sql）/ `sql_tools:<工具名>` / `plugin:<ep.name>`，运维可 `grep source=...` 按来源或类型筛。**绝不给 `db.execute` 加「把 params 写进主 logger」的能力**，否则 PII 会泄漏。
- `logging.sql_audit_file`（默认 `null`）—— 决定 audit 走"合流"还是"独立文件"。

`build_log_config` 据此实现双模式：

| `sql_audit_file` | 模式 | handler 行为 |
| --- | --- | --- |
| `null` | **合流** | audit 与 main 共用 stderr + file handler；**main handler 的 level 自动取 `min(cfg.level, cfg.sql_audit_level)`**（否则 sql_audit logger 放行的低级记录会被 handler 二次过滤掉，详见 `_min_level_name`）|
| 非空路径 | **独立** | 新增 `stderr_audit` + `file_audit` handler 实例（level 直接 = `sql_audit_level`）；audit 走这两个独立 handler；main handler 不放宽 |

**关键约束**：
- `redshift_mcp.sql_audit` logger 必须 `propagate=False` —— 否则会向父 logger `redshift_mcp` 冒泡造成重复输出。
- **绝不让两个 `RotatingFileHandler` 实例指向同一文件** —— rollover 时会竞态（拿旧 fd 继续写）。当前实现：合流模式共用一个 file handler；独立模式用不同路径，杜绝竞态。
- 所有 audit 记录同样经过 `RequestIdFilter`，含 `[rid=...]` —— 不需要为 audit 单独再挂一份。
- 失败路径（`server.py` 的 `run_sql` exception 分支）也用 `sql_audit_logger.info("失败的 SQL: ...")` 把 SQL 隔离到 audit 通道，**不走 `logger.error`**，避免 SQL 跟 traceback 一起进运行日志。

### Log 级别速查

| Level | 用途 | 实例 |
| --- | --- | --- |
| `DEBUG` | 详细诊断（项目里**没**用 `logger.debug`，但保留这一级让 uvicorn 内部诊断可放）| — |
| `INFO` | 正常事件 + graceful fallback | 连接池就绪 / 查询完成 elapsed_ms / SVV_TABLE_INFO 权限不足跳过 |
| `WARNING` | 项目暂未用 | `sql_audit_level` 的默认值（用作"过滤掉 INFO 级 SQL 输出"的闸门）|
| `ERROR`（含 `logger.exception`） | 异常路径，自动附 traceback | 工具调用失败 / 启动失败 |
| `CRITICAL` | 项目暂未用 —— 启动失败用 `logger.error` + `return N` 退出 main()，靠 systemd `Restart=on-failure` 重启 | — |

### 暴露给客户端的应用版本

在创建 `FastMCP` 实例**之后立即**显式设置 `mcp._mcp_server.version = __version__`。原因：`FastMCP.__init__` **不接受** `version` 参数，底层 `Server.version` 默认 `None`，会让 `serverInfo.version` 回落到 `importlib.metadata.version("mcp")`（这是 SDK 自身的版本，不是我们应用的）。`redshift_mcp/__init__.py` 从包元数据读应用版本，保证只有 `pyproject.toml` 一处单一来源。

如果 `mcp` SDK 升到 2.x 并在 `FastMCP.__init__` 暴露了 `version=`，把这处 `_mcp_server.version` 的赋值替换为构造器参数即可。

## Redshift / psycopg3 已编码的坑（不要轻易"清理"）

下面每一条都在开发期间真实造成过故障；改动它们前必须先读对应上下文。

1. **`psycopg._encodings.py_codecs[b"UNICODE"] = "utf-8"`** 位于 `db.py` 顶部。Redshift 把 `client_encoding` 报成 PG 7.x 遗留的名字 `UNICODE`，psycopg3 的 codec 表不认识 —— 没有这条 monkey-patch，任何借出的连接**第一次** `cur.execute()` 都会抛 `NotSupportedError: codec not available in Python: 'UNICODE'`。

2. **LIKE 模式中的 `%%`（双百分号）转义**。complex 的 SQL 由插件自有 `config.yaml` 提供（命名占位符 `%(event_date)s` / `%(limit)s`），仓库模板见 `plugins/complex/src/redshift_mcp_complex/queries/error_api.example.sql`。因为 SQL 通过 `cur.execute(sql, {...})` 调用，psycopg3 会扫描 `%X` 当作占位符 —— 字面量 `%localhost%` 会被读成 `%l`（非法占位符），psycopg3 抛 `ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders`。`plugins/complex/tests/test_sql_template.py` 守护这条坑（直接 `importlib.resources` 读那份**模板** `.sql`，连注释里都不能出现裸 `%`）。**声明式 SQL 工具（`sql_tools`）的 SQL 同样经 `cur.execute(sql, {...})` 跑，LIKE 字面 `%` 也必须写成 `%%`**。

3. **`statement_timeout` 在建连时设置**，不是每次查询时设。`db.init_pool` 把 `options=f"-c statement_timeout={ms}"` 传给 `psycopg.conninfo.make_conninfo`。之前的设计是每次调用都 `cur.execute("SET ...")`，但这种方式在某些 Redshift WLM 队列下不稳定；不要恢复回去。

4. **TCP keepalives + 连接池 `check`** 同时启用，用以抵抗 NAT/防火墙静默断连。keepalive 参数（`keepalives_idle=200` 等）配合 `ConnectionPool(check=ConnectionPool.check_connection, ...)`，让长时间空闲的连接借出后仍可用（无论是插件查询还是三件套）。完整的"空闲 / 重启 / 网络断" 行为分析归档在下文引用的 plan 文件的"参考：Redshift 连接寿命与超时层级"一节。

5. **`statement_timeout_ms` 默认 60000**（1 分钟）。是基于实测选的 —— 典型未缓存查询观察值 ~15.5s，留了 4× 安全裕度。**没有证据不要往 60s 以下调**；某个环境如果需要更宽松，通过 config 提高。

6. **`SVV_COLUMNS` 字段名是 `table_schema` / `table_name` / `table_catalog`**，不是 `schema_name` / `tablename`。Redshift 文档某些早期版本写过 `schema_name`，但实测当前集群以 `information_schema`-兼容形式命名。`db.fetch_table_columns` 的 SQL 里跑错列名会立刻抛 `UndefinedColumn`。同时 `SVV_COLUMNS` 跨 database 可见，必须加 `AND table_catalog = current_database()` 过滤，否则跨集群同名表会列定义混淆。

## 配置层细节

- pydantic 模型全部住在 `config.py`。实际配置由 `load_config(path)` 加载，没传 path 时会读 `REDSHIFT_MCP_CONFIG` 环境变量。
- **`load_config` 支持配置拆分**：顶层 `include: [glob, ...]`（**仅主配置生效，不支持嵌套**；glob 相对主配置目录、结果排序保证确定性）把片段文件按 `_deep_merge`「list 追加 / 嵌套 dict 深合并 / 标量片段覆盖」并入；`sql_tools` 条目（及主配置）的 `sql_file: x.sql` 在读取阶段就地内联成 `sql`（相对**声明它的那个文件**目录，`sql`/`sql_file` 并存或文件缺失都在此报中文错）。最终仍产出单个 `AppConfig`，对下游透明。**插件私有配置不走这里**（内聚在插件内，由插件自带的 `config.yaml` + 自实现加载器处理，见 "插件系统" 段）。
- `LoggingConfig.as_json` 字段名故意不叫 `json`，是为了避开 pydantic v2 `BaseModel.json()` 这个已废弃方法名冲突触发的 import 期警告。
- `LoggingConfig.file` 接受 `None` / `""` / 纯空白，全部归一为 `None`（即 stderr-only 模式）。归一逻辑在一个 `@field_validator(mode="before")` 里。
- `ServerConfig.auth_token` 必须是非空字符串；`ServerConfig.path` 必须以 `/` 开头。两者都有 validator 强约束并有测试覆盖。

## 通用查询能力（list_tables / describe_table / run_sql）

核心注册三件套通用查询工具，**受 `config.yaml` 的 `tables: []` 白名单约束**。它们与插件工具（如 `query_error_api_by_date`）并存，共享同一连接池 / Bearer 鉴权 / request_id 链路。

### 控制流

1. `list_tables` —— 读 `_cfg.tables` 返回 `[{name, description}, ...]`；不查 DB。
2. `describe_table(table)` —— 表名必须 `schema.table` 且在白名单（大小写不敏感，会归一到小写比对）。通过后调 `db.fetch_table_columns()`（查 `SVV_COLUMNS`）+ 可选 `db.fetch_table_info()`（查 `SVV_TABLE_INFO`），再把 `_cfg.tables[i].columns[col]` 里配置的 `description` / `example_values` 叠加进列对象。**`fetch_table_columns` 返回空列表时显式抛 `ValueError`**（表不存在 / 是 view / 权限不足都走这条路径），避免向 LLM 返回 `columns: []` 让它误以为表无列。`fetch_table_info` 在权限不足时 graceful 返回 `None`，`row_count_estimate` 字段不出现在返回里。
3. `run_sql(sql)` —— **核心安全闸门在 `sql_guard.validate_select_only`**：
   - `sqlglot.parse(sql, read="redshift")` 解析；多语句拒绝
   - 顶层必须是 `exp.Query` 子类（含 `Select` / `Union` / `Intersect` / `Except`）；`Insert/Update/Delete/Create/Drop/Alter/Set/Use/Show/Command` 一律拒绝
   - **显式拒绝任何 `SELECT INTO`**（含 `INTO TEMP` / `INTO TEMPORARY` / `#tmp` 形式）—— 用 `find_all(exp.Into)` 递归扫整个 AST，子查询里的 INTO 也抓得到
   - `ast.find_all(exp.Table)` 收集全部表引用；**先剔除 CTE 别名**（用 `find_all(exp.CTE)` 提取），再要求剩余表全部 `schema.table` 形式且在白名单
   - 通过后用 `sql_guard.apply_row_cap` 改写 AST：顶层无 `LIMIT` 时追加 `max_rows + 1`；已有 `LIMIT` 则按 `min(已有, max_rows + 1)` 收紧；**OFFSET 保留不动**（属用户分页意图）
   - 最后由 `db.aexecute(capped_sql, max_rows=...)`（唯一的 SELECT 执行入口，`params=None` / `source=None`→`host`）执行。**INFO 级完成日志只含 `source= rows= truncated= elapsed_ms=` 结构化字段**；SQL 模板 + 参数走独立的 `sql_audit` 通道（`SQL [source=host]: ... params: None`，默认 `sql_audit_level=WARNING` 时被过滤掉、切到 INFO 才输出，详见上文「SQL 审计独立通道」段；避免 LLM 写 `WHERE email='...'` 时 PII 进运行日志）

### 关键约束 / 易踩的坑

- **CTE 必须靠 `find_all(exp.CTE)` 单独提取别名集合**再用来过滤 `find_all(exp.Table)` 的结果；否则 `WITH cte AS (...) SELECT * FROM cte` 里的 `cte` 会被当成"未限定 schema 的表"而被误杀。
- **`exp.Table` 的 `.db` 属性**承载 schema 名；为空字符串（不是 None）。归一化要 `(table.db or "").lower()`。
- `apply_row_cap` 操作的是已校验的 AST，**不要在 `validate_select_only` 之外随手调它**——传入 DROP 语句的 AST 进去，输出会是合法 SQL 字符串但行为完全不对。
- 表白名单为空 `tables: []` 时三件套**仍然注册**，但所有调用都会被白名单拒绝；这是有意的：让 MCP introspection 一致地暴露工具，避免"工具时有时无"。
- `describe_table` 列说明合并的 key 用**小写**对比（`(col.get("name") or "").lower()`），所以 `config.yaml` 里写 `IP:` 和 `ip:` 等价。
- `run_sql` 的 SQL 校验错误（DML 拒绝、白名单拒绝、SELECT INTO 拒绝、语法错误）作为 `ValueError` **原样抛给客户端**，**不带 rid**（属于入参错误，含完整错误消息便于 LLM 自我纠正）。同时**记两条日志**用于运维观测：`logger.info("run_sql 拒绝: <原因>")` 进运行日志（只含拒绝原因，不含 SQL 全文 → PII 安全）；`sql_audit_logger.info("被拒绝的 SQL: <完整 SQL>")` 走 sql_audit 通道（默认 WARNING 时不落盘，运维需要时把 `sql_audit_level` 切到 INFO 才输出）。只有 DB 执行错误才走 `RuntimeError(rid + see server log)` 路径。
- **错误消息一律带 LLM 自我纠正提示**：表名不规范 / 表不在白名单 / SQL 引用非白名单表 这三类 `ValueError` 都会以 "请先调用 list_tables 查看可用表全名" 结尾，提升多轮对话恢复率。
- **`server.py` 三个工具的 DB 异常 catch 范围收窄到 `_DB_RUNTIME_ERRORS`**（`psycopg.Error` / `RuntimeError` / `ConnectionError` / `TimeoutError`），让 sqlglot 内部断言失败、TypeError / KeyError 等编程错误原样冒泡 —— FastMCP 包成 500，便于早暴露 bug。**不要把 `except Exception` 加回去**。

### 何时需要扩白名单

新增一张允许查询的表：仅改 `config.yaml` 的 `tables:` 段，加 `- name: schema.table` 即可，**不需要改代码**。要给 LLM 额外提示该表的列含义，可在 `columns:` 下加 `description` / `example_values`。

## 插件系统（两种机制）

核心包 `src/redshift_mcp/` 不含任何业务 SQL。扩展工具有**两种机制**并存：① **Python 插件**（entry_points 安装式，写代码）；② **声明式 SQL 工具**（`config.sql_tools`，零代码）。

**插件配置内聚原则**：Python 插件的私有配置**内聚在插件内部**，host `config.yaml` **不承载**插件私有配置（不存在 `plugins.config` 透传、`PluginContext` 没有 `get_plugin_config`）。`config.plugins` 只有加载开关（`enabled` / `disabled`）。

插件如需 gitignored 的外部配置（如业务 SQL、敏感参数），**自带一个 `config.yaml`（结构与 host 同构）并自行加载**，host 仍不参与。`complex` 是参考实现（见 `plugins/complex/.../_config.py`）：

- **解析优先级**：`env var（REDSHIFT_MCP_COMPLEX_CONFIG）> 插件包目录内的 config.yaml（Path(__file__).parent/config.yaml）`。默认路径按**插件包自身位置**命名空间隔离，**不复用 host 的 conf.d**（公用目录会导致插件间配置冲突）。
- **缺失即报错跳过、不静默兜底**：找不到配置时插件 `register()` 抛带期望路径 + 修复指引的中文错误，由 `load_plugins` 的 try/except 隔离、记日志、跳过本插件（不搞崩 server）。
- **打包模型（git 与 wheel 互补）**：git 仓库只提交模板 `config.example.yaml` / `queries/*.example.sql`（真实 `config.yaml` / `*.sql` 被 `.gitignore` 排除）；生产 build 时 `[tool.hatch.build]` 用 `artifacts` 把**真实** config 强制纳入 wheel、`exclude` 剔除模板 —— `uv pip install` 后默认路径即命中，连 env var 都不必。改 SQL = 改插件自有 config.yaml（重打 wheel 或用 env var 指向新配置），不必动插件代码。

  注意：`artifacts`/`exclude` 必须放 `[tool.hatch.build]` 全局层，否则 `uv build` 先打 sdist 再从 sdist 出 wheel，仅 `targets.wheel` 配的 artifacts 会被绕过。

### 机制一：Python 插件 —— 三个关键文件

- **`errors.py`** —— 只放 `DB_RUNTIME_ERRORS` 元组的零依赖叶子模块。插件要复用同一组「DB/运行时错误」分类，但不能 import `server.py`（会形成 server → plugin → 插件 → server 循环），所以提到这里；`server.py` 自己也从这里 import（`as _DB_RUNTIME_ERRORS`）。
- **`plugin.py`** —— 定义 `PluginContext`（宿主→插件契约）和 `load_plugins(ctx, *, disabled)`。加载器用 `importlib.metadata.entry_points(group="redshift_mcp.plugins")` 发现插件，**不扫描目录、不碰 `sys.path`**；每个插件的 import / 取 register / 调 register 三步各自 try/except 隔离，坏插件只记日志跳过，绝不搞崩 server。
- **`plugins/complex/`** —— 自带的参考 Demo 插件包（distribution 名 `redshift-mcp-complex`，import 包名 `redshift_mcp_complex`）。SQL 由插件**自有 `config.yaml`** 提供（`_config.py` 按 `env var > 包内约定路径` 解析，命名占位符 `%(event_date)s`/`%(limit)s`）；仓库只提交模板 `config.example.yaml` / `queries/error_api.example.sql`，真实配置 gitignored、由生产 build 打进 wheel（动了打包配置要 `unzip -l` 验证 wheel 含真实 config、不含模板）。

### 机制二：声明式 SQL 工具（`sql_tools.py`）

`register_sql_tools(ctx)` 读 `config.sql_tools`，为每条声明**动态构造一个带正确 `__signature__` + `__annotations__` 的函数**再 `mcp.add_tool` —— FastMCP 据签名推断 inputSchema（`str`→string、`int`→integer、`typing.Literal[*enum]`→enum、`Annotated[T, Field(description=...)]`→带描述）。**FastMCP 调用前用 pydantic 按 schema 校验入参**，所以工具体只需补 `date` 的 `strptime` 格式校验；执行走 `db.execute(sql, bind_dict, max_rows=...)`（命名占位符）。

- **占位符声明校验**：注册时用 `_PLACEHOLDER` 从 `spec.sql` 原文（含注释）提取所有 `%(name)s`，凡出现但未在 `params` 声明的占位符 → **记 error + 跳过该工具**。psycopg 执行时会连注释一起扫描占位符，未声明者会在运行期抛 `KeyError`；此校验把它提前到注册期（对应「坑 #2」，注释里也不能写占位符字面）。
- **可选 `date` 参数「省略取今天」**：`type=date` 且 `required=false` 且**未写显式 `default`** 的参数，调用方省略时绑定「有效时区的今天」（注册期 `datetime.now(tz).strftime(format)`，故天然合 format、无需再校验）。有效时区 = 参数级 `SqlToolParam.timezone`（若写）覆盖全局 `query.timezone`（默认 `UTC`，**两级同名**避免混淆），两者都在 config 期用 `zoneinfo.ZoneInfo` 校验 IANA 名。**显式 `default` 优先**（写了就不取今天）。`SqlToolParam` 设 `extra="forbid"`：param 上写错字段名（拼错 / 旧名）在加载期直接报错，不静默忽略回退。`_build_tool` 把这类参数的有效 `ZoneInfo` 静态解析一次、闭包捕获（`dynamic_date_tz`），并给其 schema 描述追加「省略则默认为 `<tz>` 时区的今天」。依赖 `tzdata`（最小化容器缺系统 tz 库时 `ZoneInfo` 会抛 `ZoneInfoNotFoundError`）。
- **安全闸门**：`SqlToolSpec.safe` 默认 `True` —— 注册时把 SQL（`%(name)s` 占位符先用正则替换成字面量 `1`，否则 sqlglot 解析不了）过 `sql_guard.assert_read_only`，要求单条只读查询；不通过则**跳过该工具 + 记 error**（不搞崩 server）。`safe: false` 关闭。闸门**只校验只读、不强制白名单**。
- **LIMIT 自动下推（仅 `safe=True`）**：注册期复用闸门产出的 AST 判断顶层是否有 LIMIT —— **缺则文本追加 `LIMIT (effective_max+1)`**（`effective_max = spec.max_rows or query.max_rows`）下推到 DB、记一条 info；**显式写了 LIMIT 则原样尊重、不收紧**。因 SQL 带 `%(name)s` 占位符不能复用 `apply_row_cap`（AST `.sql()` 序列化会规整原文、丢占位符），故用 `_append_limit`（另起一行拼 LIMIT、去尾部 `;`、不碰原文，对行注释结尾安全）。`safe: false` 不解析、不自动加 LIMIT（运维自负，须自带）。
- `sql_guard.assert_read_only(sql)` 是从 `validate_select_only` 抽出的只读校验（无白名单），run_sql 与 sql_tools 共用。
- 重名保护：注册前查 `_tool_manager._tools`，与核心三件套/插件工具/先前声明式工具撞名 → warn 跳过、不覆盖。
- 参数名/工具名必须是合法标识符且不以 `_` 开头（pydantic validator 拦截）；required 参数自动排前以满足 `Signature`。

### 插件契约

插件包在自己的 `pyproject.toml` 声明 entry-point（group 固定为 `redshift_mcp.plugins`）：

```toml
[project.entry-points."redshift_mcp.plugins"]
<name> = "<import_pkg>:register"
```

并暴露 `register(ctx: PluginContext) -> None`，在其中用 `@ctx.mcp.tool()` 注册工具、闭包捕获 `ctx` 拿共享资源。`PluginContext` 字段：`mcp` / `config` / `logger` / `sql_audit_logger` / `request_id_var` / `get_pool` / `aexecute` / `db_runtime_errors` / `plugin_name`，外加方法 `db_errors(operation="查询", *, logger=None)`。插件**首选** `await ctx.aexecute(sql, {bind...}, max_rows=..., source=f"plugin:{ctx.plugin_name}")` 跑只读查询（复用 host `db.aexecute` 的执行 / 计时 / 截断 / 审计，免去自管连接池；`get_pool` 留给需自管连接的特殊场景），并用 `async with ctx.db_errors(logger=log):` 把 DB 异常收成带 rid 的 `RuntimeError`（不吞编程错误）；**`db_errors` 的 `operation` 用默认中性标签即可、不放工具名 —— 客户端错误里的工具名由 FastMCP 的 `Error executing tool <name>:` 前缀自动提供**；`plugin_name` 是 host 在 `load_plugins` 里经 `dataclasses.replace` 注入的本插件 entry-point 名（插件用它做 `source` / `getChild`，**不要硬编码自名**）。**它是稳定的公开 API**：破坏性改字段会让已装插件失配，要升主版本并在 CHANGELOG 标注；新增字段 / 方法属兼容变更（升 minor）；插件应只读使用 ctx（`config` 等成员本身可变，但别改）。

### 插件分发模型（零 host 改动）

**运行期加载插件不需要改 host 的任何配置。** 发现走 `importlib.metadata.entry_points(group="redshift_mcp.plugins")`（`plugin.py` 的 `load_plugins`，不扫描目录、不碰 sys.path）：只要一个包装进了 host 的 venv 且声明了该 entry-point，启动时就被自动加载。所以**第三方插件**的正常路径是「自建 wheel → `uv pip install` 进 host 的 venv（或 `uv add`）」，host `pyproject.toml` 一字不改。

### monorepo（uv workspace）

根 `pyproject.toml` 的 `[tool.uv.workspace] members = ["plugins/*"]`（**通配**，新增 `plugins/foo/` 自动是成员、无需手写）把核心包与各插件包组织成 workspace。`[tool.uv.sources] redshift-mcp = { workspace = true }` 按**包名**匹配，一条即让所有成员对宿主的依赖走本地源（不必逐插件写）。开发期用 **`uv sync --all-packages`** 把所有成员一并 editable 装进同一 venv，使其 entry-point 在开发期生效 —— **在 repo 内新增一个自带插件无需改 root pyproject**（既不必列进 `dependency-groups.dev`，也不必加 sources 条目）。`testpaths = ["tests", "plugins/*/tests"]` 同样通配纳入各插件自带测试。生产态 `uv build` 各包、`uv pip install` 进同一 venv 即可。

### 关键约束 / 易踩的坑

- `load_plugins` 与 `register_sql_tools` 都在 `server.main()` 里 `db.init_pool()` 之后、`mcp.streamable_http_app()` 之前调用（共用一个无条件构造的 `plugin_ctx`；声明式工具独立于 `plugins.enabled` 开关）。FastMCP 的 `list_tools` 实时读 `_tool_manager._tools`、不快照，所以此时注册的工具下一次 list_tools 即可见。
- **冒烟命令只显示 3 个通用工具**：`uv run python -c "from redshift_mcp.server import mcp; ..."` 在 import 期不跑 `main()`，插件工具与声明式 SQL 工具都未注册。验证用 `uv run pytest` 或实启后调 list_tools。
- 插件与主程序**共享同一 venv**：插件能自带第三方依赖，但版本冲突会在安装期暴露。
- 插件日志统一用 `ctx.logger.getChild("<name>")`（即 `redshift_mcp.plugins.<name>`），自动冒泡到主 handler，无需单独配。
- 插件工具的错误处理沿用核心约定：DB 异常用 `ctx.db_runtime_errors` 收窄 catch、包成带 rid 的 `RuntimeError`；入参错误（如日期格式）抛裸 `ValueError`、不带 rid。
- `config.plugins.enabled=false` 整体跳过加载；`config.plugins.disabled=["name"]` 关掉单个已装插件。

## 测试有意全部离线

`tests/` 下每一条测试要么调纯函数、要么校验 pydantic 模型、要么用 `starlette.testclient` 验 Bearer 中间件、要么用 `dictConfig` + 临时文件验日志管线、要么 monkeypatch `entry_points` 验插件加载器（`tests/test_plugin_loader.py`）。**没有一条**会 import 真 DB。SQL 模板字符串校验和 `query_error_api_by_date` 的日期校验测试已随插件迁到 `plugins/complex/tests/`（后者构造一个 `PluginContext` 取出工具函数，靠未初始化的 `get_pool` 抛 RuntimeError 来验证合法日期能流转过 `strptime`）；这两个文件经根 `pyproject.toml` 的 `testpaths` 纳入 `uv run pytest`。

`tests/test_logging.py` 有个 autouse fixture 在测试之间做 3 件事（**保留它，缺一不可**）：

1. **detach 所有 handler** + close 文件 fd —— 防止前一个测试遗留的文件 handle 累积
2. **`setLevel(NOTSET)`**（root 是 `WARNING`）—— 让子 logger 恢复"继承父 logger 级别"，避免 dictConfig 留下的 `level="WARNING"` 把后续测试卡住
3. **`propagate = True`** —— **这是跨文件污染的关键**：`build_log_config` 给 `redshift_mcp` / `redshift_mcp.sql_audit` 等子 logger 都设了 `propagate=False`（防止双输出）；如果不重置，**其他测试文件的 `caplog` 抓不到这些 logger 的记录**（pytest caplog 默认靠 root handler 捕获，依赖 propagate 冒泡）

该 reset 列表当前覆盖 `redshift_mcp` / `redshift_mcp.sql_audit` / `redshift_mcp.plugins`（插件子树，兜底）/ `uvicorn{,.error,.access}` / `mcp` / root。历史教训：曾经在 `test_server_tools.py` 加了用 `caplog` 验证拒绝事件 INFO 日志的 3 条测试，单跑全过；跟 `test_logging.py` 一起跑时这 3 条立刻 fail，因为 propagate 没重置。**未来加新 logger 子树（如再增一个审计通道）时，记得把它的 name 加进这个 fixture 的 reset 列表**。

## 深度修改前值得先读的文件

- `README.md` —— 面向用户的快速入门和工具描述。
- `DEPLOY.md` —— 生产部署指南（RHEL + systemd + nginx + TLS）。
- `.claude/plans/` —— 累积的设计 / 历史 plan 文件的集合。每个非显然决定（UNICODE codec patch、`%%` 转义、超时调优、version mutation 等）都有详细 write-up，附带触发该决定的精确症状。
