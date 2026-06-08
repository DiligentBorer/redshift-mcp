"""Error API IP 统计查询的执行逻辑。

SQL 不在此硬编码，也不在 import 期读包内文件 —— 它由插件自有 ``config.yaml`` 提供（解析优先级
``env var > 包内约定路径``，见 ``_config.py``），在 ``register`` 启动时解析一次后透传进来。
模板见仓库 ``config.example.yaml`` / ``queries/error_api.example.sql``，仅在 git、不进生产 wheel。
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
    """用宿主的共享连接池对指定 event_date 跑一次 Error API IP 统计查询。

    ``sql`` 由调用方传入（来自插件 config，命名占位符 ``%(event_date)s`` / ``%(limit)s``）。
    服务端用 ``LIMIT %(limit)s``（= max_rows + 1）限制结果规模；当返回行数大于 ``max_rows`` 时把
    ``truncated`` 标为 ``True``。
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
        "error_api 查询完成 event_date=%s rows=%d truncated=%s elapsed_ms=%d",
        event_date, len(rows), truncated, elapsed_ms,
    )

    return {
        "date": event_date,
        "count": len(rows),
        "truncated": truncated,
        "rows": rows,
    }
