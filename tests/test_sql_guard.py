"""测试 sql_guard.validate_select_only 和 apply_row_cap 的核心校验逻辑。

全部离线 —— 仅校验 SQL 字符串，不连真实 Redshift。
"""
from __future__ import annotations

import pytest

from redshift_mcp.sql_guard import (
    apply_row_cap,
    assert_read_only,
    validate_select_only,
)

ALLOWED = {"analytics.events", "analytics.users"}


# ===== assert_read_only（只读校验，无白名单；供 sql_tools 安全闸门复用）=====


def test_assert_read_only_accepts_select() -> None:
    # 引用任意表（不在白名单）也通过 —— assert_read_only 不做白名单
    assert_read_only("SELECT * FROM whatever.table LIMIT 1")


def test_assert_read_only_accepts_union() -> None:
    assert_read_only("SELECT 1 UNION SELECT 2")


@pytest.mark.parametrize(
    "bad",
    [
        "DELETE FROM x",
        "DROP TABLE x",
        "UPDATE x SET a=1",
        "INSERT INTO x VALUES (1)",
        "CREATE TABLE x (a int)",
        "SELECT 1; SELECT 2",          # 多语句
        "SELECT * INTO tmp FROM x",    # SELECT INTO
        "",                            # 空
    ],
)
def test_assert_read_only_rejects_non_readonly(bad: str) -> None:
    with pytest.raises(ValueError):
        assert_read_only(bad)


# ===== 合法路径 =====


def test_simple_select_passes() -> None:
    ast = validate_select_only(
        "SELECT ip FROM analytics.events WHERE event_date='2026-05-20'",
        ALLOWED,
    )
    assert ast is not None


def test_select_with_join_passes() -> None:
    ast = validate_select_only(
        "SELECT a.ip FROM analytics.events a "
        "JOIN analytics.users u ON a.userid = u.id",
        ALLOWED,
    )
    assert ast is not None


def test_select_with_cte_passes() -> None:
    """CTE 别名不应被当作非白名单表拒绝。"""
    ast = validate_select_only(
        "WITH recent AS (SELECT * FROM analytics.users) "
        "SELECT a.ip FROM analytics.events a JOIN recent r ON a.userid = r.id",
        ALLOWED,
    )
    assert ast is not None


def test_select_with_subquery_passes() -> None:
    ast = validate_select_only(
        "SELECT ip FROM analytics.events WHERE userid IN "
        "(SELECT id FROM analytics.users WHERE country='US')",
        ALLOWED,
    )
    assert ast is not None


# ===== 拒绝路径 =====


def test_drop_rejected() -> None:
    with pytest.raises(ValueError, match="只允许查询语句"):
        validate_select_only("DROP TABLE analytics.users", ALLOWED)


def test_insert_rejected() -> None:
    with pytest.raises(ValueError, match="只允许查询语句"):
        validate_select_only("INSERT INTO analytics.users VALUES (1)", ALLOWED)


def test_update_rejected() -> None:
    with pytest.raises(ValueError, match="只允许查询语句"):
        validate_select_only("UPDATE analytics.users SET name='x'", ALLOWED)


def test_delete_rejected() -> None:
    with pytest.raises(ValueError, match="只允许查询语句"):
        validate_select_only("DELETE FROM analytics.users", ALLOWED)


def test_create_rejected() -> None:
    with pytest.raises(ValueError, match="只允许查询语句"):
        validate_select_only("CREATE TABLE x (a int)", ALLOWED)


def test_alter_rejected() -> None:
    with pytest.raises(ValueError, match="只允许查询语句"):
        validate_select_only("ALTER TABLE analytics.users ADD COLUMN x int", ALLOWED)


def test_set_rejected() -> None:
    with pytest.raises(ValueError, match="只允许查询语句"):
        validate_select_only("SET statement_timeout=1000", ALLOWED)


def test_multiple_statements_rejected() -> None:
    with pytest.raises(ValueError, match="单条"):
        validate_select_only("SELECT 1; SELECT 2", ALLOWED)


def test_unauthorized_table_rejected() -> None:
    with pytest.raises(ValueError, match="不在白名单"):
        validate_select_only("SELECT * FROM secret.users", ALLOWED)


def test_mixed_join_with_unauthorized_table_rejected() -> None:
    """JOIN 里只要混入一张非白名单表，整条 SQL 必须拒绝。"""
    with pytest.raises(ValueError, match="secret.config"):
        validate_select_only(
            "SELECT * FROM analytics.users u JOIN secret.config c ON 1=1",
            ALLOWED,
        )


def test_unqualified_table_rejected() -> None:
    """所有表必须 schema.table；裸表名一律拒绝（除非是 CTE 别名）。"""
    with pytest.raises(ValueError, match="未限定 schema"):
        validate_select_only("SELECT * FROM unqualified", ALLOWED)


def test_empty_sql_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        validate_select_only("", ALLOWED)
    with pytest.raises(ValueError, match="不能为空"):
        validate_select_only("   ", ALLOWED)


def test_invalid_sql_parse_error() -> None:
    with pytest.raises(ValueError, match="解析失败"):
        validate_select_only("not a sql at all SELECT FROM", ALLOWED)


def test_non_string_rejected() -> None:
    with pytest.raises(ValueError):
        validate_select_only(None, ALLOWED)  # type: ignore[arg-type]


def test_empty_allowed_rejects_everything() -> None:
    """白名单为空集合时任何带表的 SELECT 都拒绝。"""
    with pytest.raises(ValueError, match="不在白名单"):
        validate_select_only("SELECT * FROM analytics.users", set())


# ===== 表名归一（大小写） =====


def test_table_name_case_insensitive() -> None:
    """SQL 里大写表名要能匹配白名单里小写的归一形式。"""
    ast = validate_select_only(
        "SELECT * FROM ANALYTICS.USERS",
        {"analytics.users"},
    )
    assert ast is not None


# ===== UNION / INTERSECT / EXCEPT 合法路径（H-1 修复后） =====


def test_union_all_whitelisted_passes() -> None:
    ast = validate_select_only(
        "SELECT ip FROM analytics.users UNION SELECT ip FROM analytics.events",
        ALLOWED,
    )
    assert ast is not None


def test_union_all_with_unauthorized_table_rejected() -> None:
    """UNION 任一分支引用了非白名单表 → 整条拒绝。"""
    with pytest.raises(ValueError, match="不在白名单"):
        validate_select_only(
            "SELECT ip FROM analytics.users UNION SELECT a FROM secret.evil",
            ALLOWED,
        )


def test_intersect_whitelisted_passes() -> None:
    ast = validate_select_only(
        "SELECT ip FROM analytics.users INTERSECT SELECT ip FROM analytics.events",
        ALLOWED,
    )
    assert ast is not None


def test_except_whitelisted_passes() -> None:
    ast = validate_select_only(
        "SELECT ip FROM analytics.users EXCEPT SELECT ip FROM analytics.events",
        ALLOWED,
    )
    assert ast is not None


def test_union_with_cte_passes() -> None:
    """CTE + UNION 组合：CTE 定义里的表在白名单内，UNION 两侧表也都在。"""
    ast = validate_select_only(
        "WITH x AS (SELECT * FROM analytics.users) "
        "SELECT a FROM x UNION SELECT a FROM analytics.events",
        ALLOWED,
    )
    assert ast is not None


# ===== SELECT INTO 拒绝（H-3 修复后） =====


def test_select_into_permanent_table_rejected() -> None:
    with pytest.raises(ValueError, match="SELECT INTO"):
        validate_select_only(
            "SELECT * INTO analytics.target FROM analytics.users",
            ALLOWED,
        )


def test_select_into_temp_rejected() -> None:
    with pytest.raises(ValueError, match="SELECT INTO"):
        validate_select_only(
            "SELECT * INTO TEMP my_tmp FROM analytics.users",
            ALLOWED,
        )


def test_select_into_temporary_rejected() -> None:
    with pytest.raises(ValueError, match="SELECT INTO"):
        validate_select_only(
            "SELECT * INTO TEMPORARY my_tmp FROM analytics.users",
            ALLOWED,
        )


def test_select_into_hash_prefixed_temp_rejected() -> None:
    """Redshift 还支持 # 前缀建临时表。"""
    with pytest.raises(ValueError, match="SELECT INTO"):
        validate_select_only(
            "SELECT * INTO #my_tmp FROM analytics.users",
            ALLOWED,
        )


def test_select_into_in_subquery_rejected() -> None:
    """即便 INTO 藏在子查询里，find_all 也要能抓到。"""
    with pytest.raises(ValueError, match="SELECT INTO"):
        validate_select_only(
            "SELECT * FROM (SELECT * INTO sneaky FROM analytics.users) x",
            ALLOWED,
        )


# ===== apply_row_cap =====


def test_apply_row_cap_no_existing_limit_appends() -> None:
    ast = validate_select_only("SELECT a FROM analytics.users", ALLOWED)
    out = apply_row_cap(ast, max_rows=100)
    assert "LIMIT 101" in out


def test_apply_row_cap_existing_smaller_kept() -> None:
    """已有 LIMIT 50 < cap 101，应保留 50。"""
    ast = validate_select_only("SELECT a FROM analytics.users LIMIT 50", ALLOWED)
    out = apply_row_cap(ast, max_rows=100)
    assert "LIMIT 50" in out
    assert "LIMIT 101" not in out


def test_apply_row_cap_existing_larger_tightened() -> None:
    """已有 LIMIT 99999 > cap 101，应收紧到 101。"""
    ast = validate_select_only("SELECT a FROM analytics.users LIMIT 99999", ALLOWED)
    out = apply_row_cap(ast, max_rows=100)
    assert "LIMIT 101" in out
    assert "LIMIT 99999" not in out


def test_apply_row_cap_on_union() -> None:
    """H-1 修复必须同步 apply_row_cap 兼容 Union；否则 UNION 不会被截断。"""
    ast = validate_select_only(
        "SELECT a FROM analytics.users UNION SELECT a FROM analytics.events",
        ALLOWED,
    )
    out = apply_row_cap(ast, max_rows=100)
    assert "LIMIT 101" in out


def test_apply_row_cap_on_intersect() -> None:
    ast = validate_select_only(
        "SELECT a FROM analytics.users INTERSECT SELECT a FROM analytics.events",
        ALLOWED,
    )
    out = apply_row_cap(ast, max_rows=50)
    assert "LIMIT 51" in out


def test_apply_row_cap_preserves_offset() -> None:
    """S-5：OFFSET 是用户主动分页意图，apply_row_cap 不应改动 OFFSET。

    LIMIT 较小 (50) < cap (101) → LIMIT 保留 50；OFFSET 保留。
    """
    ast = validate_select_only("SELECT a FROM analytics.users LIMIT 50 OFFSET 1000", ALLOWED)
    out = apply_row_cap(ast, max_rows=100)
    assert "LIMIT 50" in out
    assert "OFFSET 1000" in out


def test_apply_row_cap_tightens_limit_keeps_offset() -> None:
    """LIMIT 大 (99999) > cap (101) → LIMIT 收紧；OFFSET 不变。"""
    ast = validate_select_only("SELECT a FROM analytics.users LIMIT 99999 OFFSET 100", ALLOWED)
    out = apply_row_cap(ast, max_rows=100)
    assert "LIMIT 101" in out
    assert "OFFSET 100" in out
    assert "LIMIT 99999" not in out


def test_apply_row_cap_offset_without_limit() -> None:
    """OFFSET 但无 LIMIT → 追加 cap，OFFSET 保留。"""
    ast = validate_select_only("SELECT a FROM analytics.users OFFSET 100", ALLOWED)
    out = apply_row_cap(ast, max_rows=100)
    assert "LIMIT 101" in out
    assert "OFFSET 100" in out


# ===== 三段式归一化 + 跨库越权防御（default_database）=====

# 传入 default_database 时白名单按三段式给出（两段条目已被上游补 dbname 前缀）。
ALLOWED_3 = {"mydb.analytics.events", "mydb.analytics.users"}


def test_two_part_ref_normalized_with_default_db() -> None:
    """SQL 写两段（无库前缀）→ 用 default_database 补全后命中三段式白名单。"""
    ast = validate_select_only(
        "SELECT ip FROM analytics.events", ALLOWED_3, "mydb"
    )
    assert ast is not None


def test_three_part_ref_matching_default_db_passes() -> None:
    """SQL 显式写当前库的三段式 → 与白名单一致，放行（不误杀）。"""
    ast = validate_select_only(
        "SELECT ip FROM mydb.analytics.events", ALLOWED_3, "mydb"
    )
    assert ast is not None


def test_cross_database_three_part_ref_rejected() -> None:
    """核心防御：三段式指向另一个库 → 不命中白名单 → 拒绝（跨库越权）。"""
    with pytest.raises(ValueError, match="白名单"):
        validate_select_only(
            "SELECT ip FROM otherdb.analytics.events", ALLOWED_3, "mydb"
        )


def test_default_db_is_case_insensitive() -> None:
    """default_database 与 catalog 比对大小写不敏感。"""
    ast = validate_select_only(
        "SELECT ip FROM MyDB.Analytics.Events", ALLOWED_3, "MYDB"
    )
    assert ast is not None
