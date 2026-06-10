"""测试通用查询三件套（list_tables / describe_table / run_sql）的非 DB 路径。

涉及真实 Redshift 调用的 happy path 由 README / DEPLOY 的端到端步骤覆盖。
本文件仅验证：
- 应用配置未初始化时的 RuntimeError
- 表名校验（白名单 / schema 格式）
- SQL 校验（DML / 非白名单表）
"""
from __future__ import annotations

import pytest

from redshift_mcp import server
from redshift_mcp.config import AppConfig


@pytest.fixture
def cfg_with_whitelist(monkeypatch):
    """注入一个含 tables 白名单的 AppConfig 到 server._cfg 全局。

    用 monkeypatch 保证测试结束后自动还原，互不污染。
    """
    cfg = AppConfig.model_validate(
        {
            "database": {"host": "h", "dbname": "d", "user": "u"},
            "server": {"auth_token": "t"},
            "query": {"max_rows": 100},
            "tables": [
                {"name": "analytics.users", "description": "用户主表"},
                {"name": "analytics.events"},
            ],
        }
    )
    monkeypatch.setattr(server, "_cfg", cfg)
    return cfg


# ===== list_tables =====


def test_list_tables_without_config_raises(monkeypatch) -> None:
    monkeypatch.setattr(server, "_cfg", None)
    with pytest.raises(RuntimeError, match="应用配置未初始化"):
        server.list_tables()


def test_list_tables_returns_whitelist(cfg_with_whitelist) -> None:
    result = server.list_tables()
    assert len(result) == 2
    names = {t["name"] for t in result}
    assert names == {"analytics.users", "analytics.events"}
    # 含 description 字段（可为 None）
    user_row = next(t for t in result if t["name"] == "analytics.users")
    assert user_row["description"] == "用户主表"


def test_list_tables_empty_whitelist(monkeypatch) -> None:
    cfg = AppConfig.model_validate(
        {
            "database": {"host": "h", "dbname": "d", "user": "u"},
            "server": {"auth_token": "t"},
        }
    )
    monkeypatch.setattr(server, "_cfg", cfg)
    assert server.list_tables() == []


# ===== describe_table =====


async def test_describe_table_rejects_unknown(cfg_with_whitelist) -> None:
    with pytest.raises(ValueError, match="不在白名单"):
        await server.describe_table("secret.config")


async def test_describe_table_rejects_unqualified(cfg_with_whitelist) -> None:
    with pytest.raises(ValueError, match="schema.table"):
        await server.describe_table("just_a_name")


async def test_describe_table_accepts_case_insensitive(cfg_with_whitelist, monkeypatch) -> None:
    """大写传入应被归一后命中白名单；不连真实 DB，预期最终落在
    fetch_table_columns 这一层（这里通过 mock 验证白名单校验已通过）。
    """
    called = {}

    def fake_fetch_cols(schema, table):
        called["schema"] = schema
        called["table"] = table
        # 返回至少一列 —— S-2 修复后空列会被显式拒绝
        return [{"name": "ip", "type": "varchar", "ordinal_position": 1}]

    def fake_fetch_info(schema, table):
        return None

    monkeypatch.setattr(server.db, "fetch_table_columns", fake_fetch_cols)
    monkeypatch.setattr(server.db, "fetch_table_info", fake_fetch_info)

    result = await server.describe_table("ANALYTICS.USERS")
    # 返回归一后的三段式名（两段输入用 dbname=d 补全前缀）
    assert result["name"] == "d.analytics.users"
    assert called == {"schema": "analytics", "table": "users"}


# ===== run_sql =====


async def test_run_sql_rejects_dml(cfg_with_whitelist) -> None:
    with pytest.raises(ValueError, match="只允许查询语句"):
        await server.run_sql("DROP TABLE analytics.users")


async def test_run_sql_rejects_unauthorized_table(cfg_with_whitelist) -> None:
    with pytest.raises(ValueError, match="不在白名单"):
        await server.run_sql("SELECT * FROM secret.config")


async def test_run_sql_rejects_multiple_statements(cfg_with_whitelist) -> None:
    with pytest.raises(ValueError, match="单条"):
        await server.run_sql("SELECT 1; SELECT 2")


async def test_run_sql_rejects_empty(cfg_with_whitelist) -> None:
    with pytest.raises(ValueError, match="不能为空"):
        await server.run_sql("")


async def test_run_sql_executes_on_valid(cfg_with_whitelist, monkeypatch) -> None:
    """合法 SQL 应进入到 db.execute（run_sql 走 db.aexecute → execute）；mock 验证调用链。"""
    captured = {}

    def fake_execute(sql, params=None, *, max_rows, source=None):
        captured["sql"] = sql
        captured["params"] = params
        captured["max_rows"] = max_rows
        captured["source"] = source
        return {"count": 0, "truncated": False, "columns": [], "rows": []}

    monkeypatch.setattr(server.db, "execute", fake_execute)

    result = await server.run_sql("SELECT ip FROM analytics.events WHERE event_date='2026-05-20'")
    assert result == {"count": 0, "truncated": False, "columns": [], "rows": []}
    # apply_row_cap 应已追加 LIMIT max_rows + 1 = 101
    assert "LIMIT 101" in captured["sql"]
    assert captured["max_rows"] == 100


# ===== T-2: db.py 真实 SQL 字符串快照（防回归 schema_name vs table_schema 类坑） =====


def test_fetch_table_columns_sql_uses_correct_column_names() -> None:
    """T-2：之前踩过 schema_name vs table_schema 字段名错误。

    用 inspect.getsource 锁死 SQL 里的列名 token，让未来误改立刻测试失败。
    """
    import inspect
    src = inspect.getsource(server.db.fetch_table_columns)
    # 必须用 table_schema / table_name / table_catalog（Redshift SVV_COLUMNS 标准列名）
    assert "WHERE table_catalog = current_database()" in src, \
        "应限定 table_catalog = current_database() 防跨 database 串扰"
    assert "table_schema = %s" in src, "SVV_COLUMNS 字段名应为 table_schema"
    assert "table_name = %s" in src, "SVV_COLUMNS 字段名应为 table_name"
    # 防回归：旧 bug 用过 schema_name=%s 写法，确认 SQL 里不再这么写
    # （注释里允许提到 "schema_name"，因为那是防回归说明的一部分；
    #  这里精确匹配带 %s 的赋值形式，只测真实 SQL 子串）
    assert "schema_name = %s" not in src
    assert "tablename = %s" not in src


def test_fetch_table_info_sql_uses_quoted_schema_keyword() -> None:
    """T-2：SVV_TABLE_INFO 的 "schema" / "table" 是 SQL 保留字，必须加双引号。

    源码里写成 ``"\\"schema\\" = %s"`` —— getsource 返回字符串形式后含字面 ``\\"``。
    """
    import inspect
    src = inspect.getsource(server.db.fetch_table_info)
    # 源码里 "\"schema\"" 转义形式
    assert '\\"schema\\"' in src
    assert '\\"table\\"' in src


# ===== T-3: describe_table 列说明合并大小写归一 =====


async def test_describe_table_merges_column_desc_case_insensitive(cfg_with_whitelist, monkeypatch) -> None:
    """T-3：CLAUDE.md 说 'IP:' 和 'ip:' 等价。

    config 里写小写 'ip'，DB 返回大写 'IP' 时，description 应正确叠加。
    """
    # 重新注入一个 config，列 key 用小写
    new_cfg = type(cfg_with_whitelist).model_validate({
        "database": {"host": "h", "dbname": "d", "user": "u"},
        "server": {"auth_token": "t"},
        "query": {"max_rows": 100},
        "tables": [{
            "name": "analytics.users",
            "columns": {
                "ip": {"description": "客户端 IP", "example_values": ["1.2.3.4"]},
            },
        }],
    })
    monkeypatch.setattr(server, "_cfg", new_cfg)

    # DB 返回大写列名（模拟 case-sensitive 模式或 source-of-truth 习惯）
    monkeypatch.setattr(server.db, "fetch_table_columns",
                        lambda s, t: [{"name": "IP", "type": "varchar", "ordinal_position": 1}])
    monkeypatch.setattr(server.db, "fetch_table_info", lambda s, t: None)

    result = await server.describe_table("analytics.users")
    col = result["columns"][0]
    assert col["name"] == "IP"  # DB 返回的原始 case 保留
    assert col["description"] == "客户端 IP"
    assert col["example_values"] == ["1.2.3.4"]


async def test_describe_table_rejects_empty_columns(cfg_with_whitelist, monkeypatch) -> None:
    """S-2：fetch_table_columns 返回 [] 时应明确拒绝，不返回空 columns。"""
    monkeypatch.setattr(server.db, "fetch_table_columns", lambda s, t: [])
    monkeypatch.setattr(server.db, "fetch_table_info", lambda s, t: None)

    with pytest.raises(ValueError, match="查不到任何列"):
        await server.describe_table("analytics.users")


# ===== T-4: 空白名单 / 错误消息含 list_tables 提示 =====


async def test_run_sql_empty_whitelist_message(monkeypatch) -> None:
    """T-4：tables: [] 时 run_sql 拒绝消息应含 '(空)'。"""
    from redshift_mcp.config import AppConfig
    cfg = AppConfig.model_validate({
        "database": {"host": "h", "dbname": "d", "user": "u"},
        "server": {"auth_token": "t"},
    })
    monkeypatch.setattr(server, "_cfg", cfg)
    with pytest.raises(ValueError, match=r"白名单为 \(空\)"):
        await server.run_sql("SELECT * FROM analytics.users")


async def test_error_messages_include_list_tables_hint(cfg_with_whitelist) -> None:
    """S-3：所有"表名相关错误"都应提示用户调 list_tables。"""
    # describe_table 裸表名
    with pytest.raises(ValueError, match="list_tables"):
        await server.describe_table("just_a_name")
    # describe_table 非白名单
    with pytest.raises(ValueError, match="list_tables"):
        await server.describe_table("secret.evil")
    # run_sql 非白名单
    with pytest.raises(ValueError, match="list_tables"):
        await server.run_sql("SELECT * FROM secret.evil")


# ===== run_sql 校验失败事件审计（INFO 拒绝事件 + audit 通道） =====


async def test_run_sql_rejection_emits_info_log(cfg_with_whitelist, caplog) -> None:
    """run_sql 校验失败时主 logger 应记一条 INFO 级"拒绝"事件。"""
    import logging
    with caplog.at_level(logging.INFO, logger="redshift_mcp"):
        with pytest.raises(ValueError):
            await server.run_sql("SELECT * FROM secret.evil")
    # 含拒绝原因（表名）和"run_sql 拒绝"前缀
    assert any(
        "run_sql 拒绝" in r.message and "secret.evil" in r.message
        for r in caplog.records
        if r.name == "redshift_mcp"
    )


async def test_run_sql_rejection_sql_goes_to_audit_channel(cfg_with_whitelist, caplog) -> None:
    """被拒绝的 SQL 全文走 sql_audit logger（默认 WARNING 时由 logger level 过滤；
    这里用 caplog 强制捕获 INFO 验证调用确实发生了）。"""
    import logging
    sql = "SELECT * FROM secret.evil WHERE user_id='张三'"
    with caplog.at_level(logging.INFO, logger="redshift_mcp.sql_audit"):
        with pytest.raises(ValueError):
            await server.run_sql(sql)
    # audit logger 拿到完整 SQL（含 PII —— 但只有 audit 在 INFO 级时才输出）
    audit_records = [r for r in caplog.records if r.name == "redshift_mcp.sql_audit"]
    assert audit_records, "应当至少有一条 sql_audit 记录"
    assert any(
        "被拒绝的 SQL" in r.message and "张三" in r.message
        for r in audit_records
    )


async def test_run_sql_rejection_main_log_has_no_sql_text(cfg_with_whitelist, caplog) -> None:
    """主 logger 的 INFO 拒绝事件**不应**含 SQL 全文（PII 隔离）。"""
    import logging
    sql_with_pii = "SELECT * FROM secret.evil WHERE email='boss@xxx.com'"
    with caplog.at_level(logging.INFO, logger="redshift_mcp"):
        with pytest.raises(ValueError):
            await server.run_sql(sql_with_pii)
    main_records = [r for r in caplog.records if r.name == "redshift_mcp"]
    assert main_records, "应当有 INFO 级拒绝事件"
    for r in main_records:
        # PII（邮箱）不能泄漏到主 logger
        assert "boss@xxx.com" not in r.message
        # SQL SELECT * 关键字也不应进主 logger（防 PII 间接泄漏）
        assert "SELECT *" not in r.message
