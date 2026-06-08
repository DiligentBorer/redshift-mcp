"""error_api 包内 SQL 模板的回归测试。

运行期 SQL 由插件自有 config.yaml 提供；这里守护仓库里的模板 ``queries/error_api.example.sql``
（git 提交、不进生产 wheel）的占位符 / 转义约定：

1. 模板能被 importlib.resources 读到。
2. 只用命名占位符 ``%(event_date)s`` / ``%(limit)s``，且没有未转义的裸 ``%``
   —— LIKE 模式里的字面 ``%`` 必须写成 ``%%``，否则 psycopg3 抛 ProgrammingError。
"""
from __future__ import annotations

import importlib.resources
import re


def _example_sql() -> str:
    """读包内模板 SQL（editable/workspace 下解析到源码树，恒存在）。"""
    return (
        importlib.resources.files("redshift_mcp_error_api")
        .joinpath("queries", "error_api.example.sql")
        .read_text(encoding="utf-8")
    )


def test_sql_resource_loadable() -> None:
    assert _example_sql().strip()


def test_sql_uses_named_placeholders() -> None:
    sql = _example_sql()
    assert "%(event_date)s" in sql
    assert "%(limit)s" in sql


def test_no_unescaped_bare_percent() -> None:
    # 去掉合法的 %(name)s 命名占位符与 %% 转义后，不应再有裸 %。
    stripped = re.sub(r"%\([A-Za-z_]\w*\)s", "", _example_sql()).replace("%%", "")
    assert "%" not in stripped, (
        "SQL 含未转义的裸 '%'；LIKE 模式里的字面量 '%' 必须写成 '%%'。"
    )
