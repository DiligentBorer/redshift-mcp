"""Error API IP 统计查询的固定 SQL 与执行逻辑。

本模块无任何 MCP / FastMCP 依赖，便于离线单测（``test_sql_template`` 直接 import
``SQL_TEMPLATE`` 做正则校验）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from psycopg_pool import ConnectionPool

# 注意：末尾的 LIMIT %s 让 Redshift 在服务端就截断结果集，避免把巨大
# 结果集拉回本地再丢弃浪费出口带宽。实际传入 max_rows + 1，若返回 >
# max_rows 即可判定发生了截断。
#
# 坑：因为 SQL 通过 cur.execute(SQL_TEMPLATE, (params,)) 调用，psycopg3 会把 %X
# 当占位符扫描。LIKE 模式里的字面量 '%' 必须写成 '%%' 转义，否则 '%localhost%'
# 会被读成 '%l'（非法占位符）抛 ProgrammingError。
SQL_TEMPLATE = """
SELECT DISTINCT ip, COUNT(DISTINCT devicekey) AS dknumbers
FROM dwd.t_action_info_widen
WHERE ((category = 'ERROR_API' AND action ~* '-1|408')
       OR category = 'ERROR_API_Performance')
  AND sourcelocation NOT LIKE '%%localhost%%'
  AND sourcelocation NOT LIKE '%%uat.flamingo.shop%%'
  AND sourcelocation NOT LIKE '%%test.flamingo.shop%%'
  AND country = 'US'
  AND us_day = %s
GROUP BY ip
ORDER BY dknumbers DESC
LIMIT %s
"""


def run_query(
    pool: ConnectionPool,
    us_day: str,
    *,
    max_rows: int,
    logger: logging.Logger,
) -> dict[str, Any]:
    """用宿主的共享连接池对指定 us_day 跑一次 Error API IP 统计查询。

    服务端用 SQL ``LIMIT max_rows + 1`` 限制结果规模；当返回行数大于
    ``max_rows`` 时把 ``truncated`` 标为 ``True``。
    """
    t0 = time.monotonic()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_TEMPLATE, (us_day, max_rows + 1))
            rows = cur.fetchall()
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]

    logger.info(
        "error_api 查询完成 us_day=%s rows=%d truncated=%s elapsed_ms=%d",
        us_day, len(rows), truncated, elapsed_ms,
    )

    return {
        "date": us_day,
        "count": len(rows),
        "truncated": truncated,
        "rows": rows,
    }
