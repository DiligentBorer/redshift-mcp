# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。

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
