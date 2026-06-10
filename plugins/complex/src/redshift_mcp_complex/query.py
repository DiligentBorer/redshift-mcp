"""Error API IP 统计查询的执行逻辑 —— **「插件自管连接池执行」的备选参考实现**。

当前 ``register()`` 走 host 的 ``ctx.aexecute``（推荐路径，复用 db.execute 的执行 / 计时 / 截断 /
审计）；**本函数未被接线**，保留下来给后续开发者对照「如何用 ``ctx.get_pool()`` 低层自管执行」。

SQL 由插件自有 ``config.yaml`` 提供（解析优先级 ``env var > 包内约定路径``，见 ``_config.py``），
在 ``register`` 启动时解析一次后透传进来；模板见 ``config.example.yaml`` / ``queries/*.example.sql``。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from psycopg_pool import ConnectionPool


def run_query(
    pool: ConnectionPool,
    event_date: str,
    *,
    sql: str,
    max_rows: int,
    logger: logging.Logger,
) -> dict[str, Any]:
    """用宿主的共享连接池对指定 event_date 跑一次 Error API IP 统计查询（备选参考、未接线）。

    ``sql`` 由调用方传入（来自插件 config，命名占位符 ``%(event_date)s`` / ``%(limit)s``）。
    服务端用 ``LIMIT %(limit)s``（= max_rows + 1）限制结果规模；当返回行数大于 ``max_rows`` 时把
    ``truncated`` 标为 ``True``。``logger`` 名已带插件身份，故消息不再前缀插件自名。
    """
    t0 = time.monotonic()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {"event_date": event_date, "limit": max_rows + 1})
            rows = cur.fetchall()
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]

    logger.info(
        "查询完成 event_date=%s rows=%d truncated=%s elapsed_ms=%d",
        event_date, len(rows), truncated, elapsed_ms,
    )

    return {
        "count": len(rows),
        "truncated": truncated,
        "rows": rows,
    }
