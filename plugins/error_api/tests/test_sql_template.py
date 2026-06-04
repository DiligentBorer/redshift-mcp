"""error_api 包内 SQL 的回归测试（SQL 现内聚在 queries/error_api.example.sql，importlib.resources 读取）。

守护两件事：
1. SQL 资源能被读到（防打成 wheel 时漏掉 .sql）。
2. 只用命名占位符 `%(event_date)s` / `%(limit)s`，且没有未转义的裸 `%`
   —— LIKE 模式里的字面 `%` 必须写成 `%%`，否则 psycopg3 抛 ProgrammingError。
"""
from __future__ import annotations

import importlib.resources
import re

from redshift_mcp_error_api.query import SQL


def test_sql_resource_loadable() -> None:
    """importlib.resources 能读到包内 .example.sql（防打包漏文件）。"""
    res = importlib.resources.files("redshift_mcp_error_api").joinpath(
        "queries", "error_api.example.sql"
    )
    assert res.read_text(encoding="utf-8").strip()


def test_sql_uses_named_placeholders() -> None:
    assert SQL.strip()
    assert "%(event_date)s" in SQL
    assert "%(limit)s" in SQL


def test_no_unescaped_bare_percent() -> None:
    # 去掉合法的 %(name)s 命名占位符与 %% 转义后，不应再有裸 %。
    stripped = re.sub(r"%\([A-Za-z_]\w*\)s", "", SQL).replace("%%", "")
    assert "%" not in stripped, (
        "SQL 含未转义的裸 '%'；LIKE 模式里的字面量 '%' 必须写成 '%%'。"
    )
