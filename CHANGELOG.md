# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.3.7] - 2026-07-09

修复公共 Release 只附 host wheel、下载后装不上的问题(0.3.6 抽 SDK 引入),升 patch。

### Fixed

- **Release 一并发布 `redshift-mcp-sdk` wheel**:host 的 `Requires-Dist` 依赖 `redshift-mcp-sdk`
  而它未发 PyPI;0.3.6 的 Release 只附 host wheel,下载后 `pip install redshift_mcp-*.whl` 会因
  解析不到 sdk 而失败。`release.yaml` 改为同时 `uv build --package redshift-mcp-sdk`,两个 wheel/sdist
  一并附到 GitHub Release;README / DEPLOY 的 wheel 安装(含升级/回滚)改为把两者放同一目录后
  `uv pip install --find-links <目录> redshift-mcp`。GHCR 镜像不受影响(本就自包含)。

## [0.3.6] - 2026-07-08

抽出插件契约层 SDK `redshift-mcp-sdk`,并**移除仓内 demo 插件**:host 收敛为「通用 Redshift MCP
server + 插件框架」,所有业务插件都是外部独立包。host / sdk 版本统一为 0.3.6。

### Added

- **新增 workspace 成员 `sdk/`(distribution `redshift-mcp-sdk`)**:插件契约层(`PluginContext` /
  `GROUP` / `errors`)从 host 抽出成独立薄包,外部插件只 `from redshift_mcp_sdk import PluginContext`
  即可,不引入 host 实现源码。SDK 暴露 `__version__`;psycopg 为可选 extra(缺失时 `DB_RUNTIME_ERRORS`
  守卫式降级)。host 的 `redshift_mcp.plugin` / `redshift_mcp.errors` 从 SDK re-export 同一对象作 back-compat。
- **host 启动期 SDK 版本断言**:`server._check_sdk_version()` 在装了低于 `_MIN_SDK_VERSION` 的旧 SDK
  时启动即报清晰中文错误并退出(exit code 4),而非等到插件 register 时才 `AttributeError`。

### Removed

- **移除仓内 demo 插件 `plugins/complex/`(及其一切痕迹)**:host 不再夹带任何 in-repo 插件。业务插件
  改由**外部独立仓**维护(自带 gitignored 业务 SQL、自建 wheel、`uv pip install` 进 host venv 即被
  entry_points 发现)。`[tool.uv.workspace] members` 收敛为 `["sdk"]`,`testpaths` 收敛为 `["tests"]`。
  Dockerfile 去掉插件捆绑分支(`INSTALL_*` build-arg),镜像纯 host;插件由外部 wheel 事后装入。
  > 升级须知:若此前依赖仓内 demo 插件,请改用对应的外部插件包 `uv pip install`。

### Changed

- **host 依赖 SDK 用区间 `redshift-mcp-sdk>=0.3,<1.0`**:下限=构建时契约版本、上限=下一主版本
  (SDK 主版本内严格向后兼容),让共享 venv 里 SDK 安全上浮、不兼容在安装期即暴露。host 新增显式依赖
  `packaging>=21`(启动断言做版本比较用)。
- **Dockerfile**:runtime 安装补 `--find-links /tmp/dist`,让本地 `redshift-mcp-sdk` wheel 可解析
  (SDK 未发 PyPI,host 的 `Requires-Dist` 含它);`--all-packages` 现只产出 host + sdk 两个 wheel。

## [0.3.5] - 2026-07-07

声明式 SQL 工具增强:可选 date 参数默认取当前时区今天 + 占位符声明校验,升 patch。

### Added

- **可选 `date` 参数「省略取今天」**:声明式 SQL 工具里 `type=date` 且 `required=false` 且未写
  `default` 的参数,调用方省略时按有效时区绑定当天日期。有效时区 = 参数级 `timezone` 覆盖全局
  `query.timezone`(默认 `UTC`,均用 `zoneinfo.ZoneInfo` 校验 IANA 名);写了显式 `default` 则以
  `default` 为准。适合「不传就查当天」的日报类工具。
- **新增配置项 `query.timezone`(全局)与 `SqlToolParam.timezone`(参数级)**:两级同名,后者覆盖前者。
- **依赖 `tzdata`**:最小化容器缺系统时区库时 `ZoneInfo` 会抛 `ZoneInfoNotFoundError`,带上以保证
  任何环境可解析 IANA 时区名。

### Changed

- **`SqlToolParam` 改为 `extra="forbid"`**:param 上写了未知字段(拼错 / 旧字段名)在配置加载期
  **直接报错**,不再静默忽略再回退默认。若既有配置的 param 带过多余或拼错字段(此前被静默忽略),
  升级后需清理。

### Fixed

- **声明式 SQL 工具注册期校验未声明占位符**:psycopg 执行时会扫描整个 SQL(**含注释**)寻找
  `%(name)s` 占位符;注释里出现但未在 `params` 声明的占位符会让运行期抛 `KeyError`。改为注册期
  提取 SQL 里所有占位符与 `params` 声明比对,有未声明者记 error 并跳过该工具,不搞崩 server。

## [0.3.4] - 2026-07-01

仅发布流水线(CI)加固,代码与运行行为不变,升 patch。

### Changed

- **`latest` 镜像 tag 只在正式 tag 打**:prerelease(如 `v0.3.4-rc1`,ref 名含 `-`)不再覆盖
  `ghcr.io` 的 `latest` / `major.minor` 系列 tag,避免预发布镜像被误当稳定版拉取。
- **`setup-uv` 固定到 `v8.2.0`**:改用不可移动的精确版本 tag,消除对会漂移的 `v8` 大版本 tag 的依赖,
  保证发布流水线可复现。
- **发布 workflow 的 action 升级到 Node 24**:`checkout` / `metadata-action` 等升级到基于 Node 24
  运行时的版本,跟随 GitHub Actions runner 的弃用节奏。

## [0.3.3] - 2026-06-25

可观测性与结构改进,核心查询行为不变,升 patch。

### Added

- **日志每请求独立 rid、全链路可追踪**:消除"会话级 rid"——以往同一会话内不同请求的处理日志
  (含 SDK 的 `Processing request of type X`、`查询完成`、错误、审计)复用建会话时的 rid;现在
  每个 HTTP 请求(`tools/call`/`tools/list`/`GET`/`DELETE`)全程带各自的 rid。实现:`server.py`
  防御式包一层 lowlevel server 的 `_handle_request`,在每条消息处理入口按"发起该消息的请求"设 rid。
- **日志在 rid 旁同时输出 `sid`(会话 id,前 8 位)**:把"同一会话的多个请求"串起来。rid 与 sid 由
  同一 filter 同时设置、同一格式串渲染,**永远成对出现**;`sid` 是 SDK 完整 session id 的前缀,
  `grep sid=xxxxxxxx` 可同时命中我方行与 SDK 的 `session ID: <完整>` 行。文本前缀
  `[rid=.. sid=..]`,JSON 加 `session_id` 字段。

### Changed

- **抽出 `middleware.py`(纯结构重构,行为不变)**:把 HTTP 接入层(`request_id_var` /
  `RequestIdFilter` / `RequestIdMiddleware` / `BearerAuthMiddleware` + initialize body 辅助)从
  `server.py` 移到独立模块,`server.py` 回归"日志组装 + 工具 + main"。`request_id_var` 随中间件
  同迁(被多处共用,留在 server 会成环)。

## [0.3.2] - 2026-06-24

可观测性改进,核心查询行为不变,升 patch。

### Added

- **`logging.uvicorn_access_log` 开关**（默认 `true`）：控制 uvicorn 自身的 HTTP 访问流水日志。
  放在 nginx 等反代之后时可设 `false` 关掉，避免与反代的访问日志重复 + 噪音（反代后 uvicorn 只看得到
  代理 IP）。仅关该项，不影响 `uvicorn.error` 与带 rid 的业务日志。
- **会话建立日志带来源客户端**：MCP `initialize` 时补记一行
  `会话建立 session=.. client=..`（client 取自 initialize 的 `clientInfo`，与 SDK 的
  `Created new transport` 同 rid / 同 session），便于多个客户端连同一 server 时按来源区分。

### Changed

- **`RequestIdMiddleware` 重写为纯 ASGI**：在保持 rid 染色 + 回写 `X-Request-ID`（401 也带）的基础上，
  合并上面的会话/client 日志 —— 取 clientInfo 需读 initialize 请求体并安全回放给下游，纯 ASGI 才能做到。

## [0.3.1] - 2026-06-24

发布与分发层面的改进，核心代码无变更，故升 patch。

### Added

- **公开发布 workflow（GHCR + Release）**：打 `v*` tag 时构建 runtime 镜像推 GHCR，并把主包
  `wheel` / `sdist` 附到 GitHub Release 供直接下载（说明由 `generate_release_notes` 自动生成）。
- **多架构镜像**：GHCR 镜像同时构建 `linux/amd64` + `linux/arm64`，`docker pull` 自动按本机架构选层
  —— Apple Silicon 等 arm64 机器原生运行，消除 platform mismatch warning。
- **README**：新增「用 Docker 运行（GHCR 镜像）」一节（`docker run` / `docker-compose` / 配置挂载说明）；
  安装段补充从 Releases 页面下载主包 wheel 的入口。

### Changed

- **公共镜像不再内置 `complex` 插件**：`runtime` 阶段默认只装主包；私有线可用
  `--build-arg INSTALL_COMPLEX=1` 按需 opt-in。与「插件按需安装」模型一致，避免把含业务 SQL 的
  插件打进公开镜像。

## [0.3.0] - 2026-06-10

聚焦「功能 / 逻辑层面的复用」的一轮重构，对 `PluginContext` 公开 API 为**新增（向后兼容）**，故升 minor。

### Added

- **`PluginContext.aexecute`**（字段，= `db.aexecute`）：插件执行只读查询的**首选入口**，复用 host 的
  执行 / 计时 / 行截断 / SQL 审计，免去自管连接池。`get_pool` 保留给需自管连接的特殊场景。
- **`PluginContext.plugin_name`**（字段）：本插件的 entry-point 名，由 `load_plugins` 经
  `dataclasses.replace` 注入。插件用它做 `source` 标识 / `getChild` 子 logger，避免硬编码自名。
- **`PluginContext.db_errors(operation="查询", *, logger=None)`**（方法）：返回 async 上下文管理器，把
  `db_runtime_errors` 范围内的异常收成带 rid 的 `RuntimeError`（不吞编程错误），供插件复用、免抄样板。
  `operation` 是面向客户端/日志的标签、默认中性「查询」即可——客户端错误里的工具名由 FastMCP 的
  `Error executing tool <name>:` 前缀自动提供，**无需也不应把工具名放进 `operation`**。
- **`errors.db_errors_as_client_error`**：上面方法背后的零依赖叶子实现。
- **`db.execute`/`aexecute` 新增 `source` 参数**：标识查询来源（`host` / `sql_tools:<工具名>` /
  `plugin:<ep.name>`），进完成日志与审计行，运维可 `grep source=...` 按来源筛（仅进审计/日志，不进客户端消息）。
- **`config.split_table_ref`**：`schema.table` / `database.schema.table` 解析 + 校验，供 `describe_table`
  与 `TableSpec` 名称校验共用。
- **CLI**：`redshift-mcp --list-plugins`（简写 `-l`，免启动、不读 config、不连 DB，列出已装插件
  `ep.name / distribution / version`，方便查 `plugins.disabled` 该填什么名）；`--version`/`-V` 打印版本；
  `--help` 带用例 epilog。

### Changed

- **统一 SQL 执行入口**：`db.execute`（其 async 封装 `db.aexecute`）成为 `run_sql` / 声明式 sql_tools /
  插件**唯一**的 SELECT 执行入口；移除冗余的 `db.query_sql` / `db.aquery_sql`（`execute(sql, params=None)`
  即其等价）。`run_sql` 改走 `db.aexecute`。连接借出 / 游标样板抽成私有 `db._select`，由 `execute` 与
  `fetch_table_columns` / `fetch_table_info` 共用。
- **统一 SQL 审计**：所有 SELECT 执行都经 `db.execute` 统一向 `sql_audit_logger` 记
  `SQL [source=<x>]: <模板> params: <bind>`（默认仍被 `sql_audit_level=WARNING` 闸住、PII 安全）。
  补足了原先 `execute` 路径（sql_tools / 插件查询）完全不审计的缺口。**参数仅走审计通道、绝不进主 logger。**
- INFO 级完成日志标签统一为 `查询完成 source=... rows=... truncated=... elapsed_ms=...`。
- 参考插件 `error_api` **改用 `ctx.aexecute` + `ctx.db_errors`**，删除手写的连接 / 执行样板；
  `query.py::run_query` 保留为「插件自管连接池执行」的备选参考（未接线）。其工具返回去掉 `date` 字段
  （改返回 `db.execute` 原生的 `{count, truncated, columns, rows}`）。

### Renamed

- 参考插件全套改名：distribution `redshift-mcp-error-api` → **`redshift-mcp-complex`**；import 包
  `redshift_mcp_error_api` → **`redshift_mcp_complex`**；entry-point 名 `error_api` → **`complex`**；
  env var `REDSHIFT_MCP_ERROR_API_CONFIG` → **`REDSHIFT_MCP_COMPLEX_CONFIG`**。
  MCP 工具名 `query_error_api_by_date` **保持不变**（LLM 可见契约）。
  > 升级须知：`plugins.disabled` 若曾写 `error_api`，改为 `complex`；自定义 env var 同步改名。
