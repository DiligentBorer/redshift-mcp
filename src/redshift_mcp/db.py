from __future__ import annotations

import logging
import time
from functools import partial
from typing import Any

import anyio
import psycopg
import psycopg._encodings as _pg_enc
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DatabaseConfig

# Redshift 把 client_encoding 报成 PG 7.x 遗留的名字 'UNICODE'，
# psycopg3 的 codec 表不认识它，会在第一次 execute() 时抛 NotSupportedError。
# 这里把它别名为 utf-8。
_pg_enc.py_codecs.setdefault(b"UNICODE", "utf-8")

logger = logging.getLogger(__name__)
# SQL 审计专用子 logger —— 仅用来记录 run_sql 执行的完整 SQL 文本。
# 与主 logger 独立控制 level（由 LoggingConfig.sql_audit_level 决定），
# 让运维可以 "level=INFO + sql_audit_level=INFO" 只看 SQL 不放出 uvicorn 噪音；
# 也可以配 LoggingConfig.sql_audit_file 让审计走独立文件。详见 server.build_log_config。
sql_audit_logger = logging.getLogger("redshift_mcp.sql_audit")

_pool: ConnectionPool | None = None


def init_pool(cfg: DatabaseConfig, statement_timeout_ms: int) -> None:
    """构建 psycopg 连接池。

    - ``statement_timeout`` 通过 libpq 的 ``options`` 在建连时设置，
      避免每次查询前多发一条 ``SET``（某些 Redshift WLM 队列下该写法不稳定）。
    - TCP keepalives 让长连接穿越会静默断空闲 TCP 的 NAT/防火墙
      （跨 VPC 访问 Redshift 时常见）依然存活。
    - 连接池的 ``check`` 回调在借出连接前会快速跑一次 ``SELECT 1``，
      避免半关闭 socket 在第一次调用时表现为 OperationalError。
    """
    global _pool
    if _pool is not None:
        return
    conninfo = psycopg.conninfo.make_conninfo(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.dbname,
        user=cfg.user,
        password=cfg.password,
        sslmode=cfg.sslmode,
        connect_timeout=cfg.connect_timeout,
        options=f"-c statement_timeout={int(statement_timeout_ms)}",
        keepalives=1,
        keepalives_idle=200,
        keepalives_interval=10,
        keepalives_count=5,
    )
    _pool = ConnectionPool(
        conninfo=conninfo,
        min_size=cfg.pool_min_size,
        max_size=cfg.pool_max_size,
        kwargs={"row_factory": dict_row},
        check=ConnectionPool.check_connection,
        open=True,
    )
    _pool.wait(timeout=cfg.connect_timeout)
    logger.info(
        "Redshift 连接池就绪 (host=%s port=%s db=%s min=%d max=%d "
        "statement_timeout_ms=%d)",
        cfg.host, cfg.port, cfg.dbname, cfg.pool_min_size, cfg.pool_max_size,
        statement_timeout_ms,
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_pool() -> ConnectionPool:
    """返回共享连接池；未初始化时抛 ``RuntimeError``。

    供插件经 ``PluginContext.get_pool`` 拿到与主程序同一个连接池跑自己的
    参数化 SQL，无需各自维护连接逻辑。
    """
    if _pool is None:
        raise RuntimeError("连接池未初始化")
    return _pool


# ---- 通用查询能力（list_tables / describe_table / run_sql）----


def fetch_table_columns(schema: str, table: str) -> list[dict[str, Any]]:
    """从 Redshift 拉取指定表的列定义。

    用 ``SVV_COLUMNS`` 视图（Redshift 提供，跨 schema/database 可见，
    不需要在表所在的 schema 下也能查）。返回字段固定为
    ``{name, type, ordinal_position}``，方便上层叠加 config 里的说明。
    """
    pool = get_pool()

    # Redshift SVV_COLUMNS 实际列名为 table_schema（不是 schema_name），
    # 跑错列名会立刻抛 UndefinedColumn。
    # 同时 SVV_COLUMNS 跨 database 可见 —— 必须 table_catalog 过滤当前 database，
    # 否则另一个 database 里同名 schema.table 的列定义会被混进结果。
    sql = (
        "SELECT column_name AS name, "
        "       data_type   AS type, "
        "       ordinal_position "
        "FROM SVV_COLUMNS "
        "WHERE table_catalog = current_database() "
        "  AND table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position"
    )
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (schema, table))
            return cur.fetchall()


def fetch_table_info(schema: str, table: str) -> dict[str, Any] | None:
    """从 ``SVV_TABLE_INFO`` 拿表的行数估计；权限不足 / 找不到时返回 ``None``。

    ``SVV_TABLE_INFO`` 默认只有超级用户能 SELECT；只读账号会抛
    ``InsufficientPrivilege``。这里 catch 住 ``psycopg.Error``、记一条 info
    级别日志后返回 None —— 让 ``describe_table`` 不因此整体失败，
    ``row_count_estimate`` 字段自然不出现在返回里。
    """
    pool = get_pool()

    sql = (
        "SELECT tbl_rows AS row_count_estimate, size AS mb_size "
        "FROM SVV_TABLE_INFO "
        "WHERE \"schema\" = %s AND \"table\" = %s"
    )
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (schema, table))
                rows = cur.fetchall()
        return rows[0] if rows else None
    except psycopg.Error as exc:
        logger.info(
            "fetch_table_info 跳过 schema=%s table=%s err=%s",
            schema, table, exc.__class__.__name__,
        )
        return None


def query_sql(sql: str, *, max_rows: int) -> dict[str, Any]:
    """通用 SELECT 执行入口。

    **传入的 ``sql`` 必须已经过 ``sql_guard.validate_select_only`` 校验，
    且建议用 ``sql_guard.apply_row_cap`` 包过 LIMIT 才传进来**；本函数只负责
    实际执行与组装返回值，不做 SQL 安全校验。
    """
    pool = get_pool()

    t0 = time.monotonic()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            columns = [d.name for d in cur.description] if cur.description else []
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]

    # INFO 级别只记结构性字段，避免 LLM 写的 WHERE 子句里的 PII（邮箱 /
    # 用户名 / 手机号）泄漏到运行日志。完整 SQL 走独立的 sql_audit logger，
    # 由 LoggingConfig.sql_audit_level / sql_audit_file 独立控制。
    logger.info(
        "run_sql 完成 rows=%d truncated=%s elapsed_ms=%d",
        len(rows), truncated, elapsed_ms,
    )
    sql_audit_logger.info("SQL: %s", sql)

    return {
        "count": len(rows),
        "truncated": truncated,
        "columns": columns,
        "rows": rows,
    }


def execute(
    sql: str,
    params: tuple[Any, ...] | dict[str, Any] | None = None,
    *,
    max_rows: int,
) -> dict[str, Any]:
    """通用参数化 SELECT 执行入口（供插件复用）。

    纯机制：取池 → 执行（可带参数）→ fetchall → 计时 → 行数截断 → INFO 日志，
    返回 ``{count, truncated, columns, rows}``。

    **不做 SQL 安全校验**（调用方自负安全），也**不打 sql_audit**：插件的 SQL
    是硬编码的、没有用户输入拼接的 PII 注入面，区别于 ``run_sql`` 走的
    ``query_sql``。需要审计的插件可自行用 ``sql_audit_logger`` 记录。
    """
    pool = get_pool()
    t0 = time.monotonic()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [d.name for d in cur.description] if cur.description else []
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]

    logger.info(
        "execute 完成 rows=%d truncated=%s elapsed_ms=%d",
        len(rows), truncated, elapsed_ms,
    )

    return {
        "count": len(rows),
        "truncated": truncated,
        "columns": columns,
        "rows": rows,
    }


# ---- async 封装：把上面的阻塞 psycopg 调用丢到 worker 线程 ----
#
# FastMCP 对同步工具是 inline 执行（func_metadata 里 `return fn(...)`，不走线程池），
# 单 worker / 单事件循环下一条慢查询会阻塞整个 loop、串行化所有并发请求。工具改成
# async 后用下面的封装把阻塞 I/O 丢到线程，事件循环得以继续处理其它请求 / SSE 心跳。
# anyio.to_thread.run_sync 会把当前 contextvars 复制进线程，因此 request_id 等仍可读
# （不过本项目的 rid 都在协程里先 .get() 再调用，不依赖这一点）。
# 同步实现保持不变，供这些封装与（如有）同步调用方共用。


async def aquery_sql(sql: str, *, max_rows: int) -> dict[str, Any]:
    """``query_sql`` 的 async 封装（阻塞执行丢到 worker 线程）。"""
    return await anyio.to_thread.run_sync(partial(query_sql, sql, max_rows=max_rows))


async def aexecute(
    sql: str,
    params: tuple[Any, ...] | dict[str, Any] | None = None,
    *,
    max_rows: int,
) -> dict[str, Any]:
    """``execute`` 的 async 封装（阻塞执行丢到 worker 线程）。"""
    return await anyio.to_thread.run_sync(partial(execute, sql, params, max_rows=max_rows))


async def afetch_table_columns(schema: str, table: str) -> list[dict[str, Any]]:
    """``fetch_table_columns`` 的 async 封装。"""
    return await anyio.to_thread.run_sync(fetch_table_columns, schema, table)


async def afetch_table_info(schema: str, table: str) -> dict[str, Any] | None:
    """``fetch_table_info`` 的 async 封装。"""
    return await anyio.to_thread.run_sync(fetch_table_info, schema, table)
