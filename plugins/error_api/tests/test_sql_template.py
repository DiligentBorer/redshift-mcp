"""SQL 模板占位符规范的回归测试。

psycopg3 只允许 %s/%b/%t 这几类占位符。LIKE 模式里的字面量 '%'
必须写成 '%%'。之前曾出现过 `%localhost%` 被解析成 '%l'（非法占位符）
的 bug；本测试用于防止该回归。
"""
from __future__ import annotations

import re

from redshift_mcp_error_api.query import SQL_TEMPLATE

# psycopg3 只允许下列占位符；%% 为字面 percent 的转义形式。
ALLOWED = {"s", "b", "t", "%"}


def test_sql_template_has_only_allowed_placeholders() -> None:
    tokens = re.findall(r"%(.)", SQL_TEMPLATE)
    forbidden = [t for t in tokens if t not in ALLOWED]
    assert forbidden == [], (
        f"SQL_TEMPLATE 含有非法占位符 {forbidden}; "
        f"LIKE 模式里的字面量 '%' 必须转义为 '%%'。"
    )


def test_sql_template_has_exactly_two_parameter_slots() -> None:
    """一个给 us_day，一个给 LIMIT max_rows+1。"""
    tokens = re.findall(r"%(.)", SQL_TEMPLATE)
    param_slots = [t for t in tokens if t == "s"]
    assert len(param_slots) == 2, (
        f"期望恰好 2 个 %s 占位符（us_day + LIMIT），实际 {len(param_slots)} 个"
    )


def test_sql_template_contains_limit_clause() -> None:
    assert "LIMIT %s" in SQL_TEMPLATE, "必须包含 LIMIT 子句，在服务端就截断结果规模"
