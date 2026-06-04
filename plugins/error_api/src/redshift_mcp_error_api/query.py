"""Error API IP 统计查询的执行逻辑。

SQL 不再硬编码在此 —— 它内聚在包内 ``queries/error_api.sql``，由 ``importlib.resources``
读取（配置内聚在插件内部，不侵入 host config.yaml）。改 SQL = 改那个 .sql 文件。
"""
from __future__ import annotations

import importlib.resources
import logging
import time
from typing import Any

from psycopg_pool import ConnectionPool

# 包内自带的 SQL（package data）。命名占位符 %(event_date)s / %(limit)s。
# 注：importlib.resources 在 editable 安装下直接读源码树；打成 wheel 后需确保 .sql 进包
# （见插件 pyproject 的 hatch 配置 / 用 unzip -l 验证）。
SQL: str = (
    importlib.resources.files(__package__)
    .joinpath("queries", "error_api.sql")
    .read_text(encoding="utf-8")
)


def run_query(
    pool: ConnectionPool,
    event_date: str,
    *,
    max_rows: int,
    logger: logging.Logger,
) -> dict[str, Any]:
    """用宿主的共享连接池对指定 event_date 跑一次 Error API IP 统计查询。

    服务端用 SQL ``LIMIT %(limit)s``（= max_rows + 1）限制结果规模；当返回行数大于
    ``max_rows`` 时把 ``truncated`` 标为 ``True``。
    """
    t0 = time.monotonic()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SQL, {"event_date": event_date, "limit": max_rows + 1})
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
