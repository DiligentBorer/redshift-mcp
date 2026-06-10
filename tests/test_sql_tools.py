"""声明式 SQL 工具（sql_tools.register_sql_tools）测试 —— 全离线。

用真实 `FastMCP("test")` 实例验证动态注册 + inputSchema；monkeypatch `db.execute`
验证参数绑定 / 错误包装，不连真实 DB。
"""
from __future__ import annotations

import contextvars
import logging

import pytest
from mcp.server.fastmcp import FastMCP

from redshift_mcp import db
from redshift_mcp.config import AppConfig
from redshift_mcp.plugin import PluginContext
from redshift_mcp.sql_tools import register_sql_tools


def _ctx(tools: list[dict], *, max_rows: int = 100) -> PluginContext:
    cfg = AppConfig.model_validate(
        {
            "database": {"host": "h", "dbname": "d", "user": "u"},
            "server": {"auth_token": "t"},
            "query": {"max_rows": max_rows},
            "sql_tools": tools,
        }
    )
    return PluginContext(
        mcp=FastMCP("test"),
        config=cfg,
        logger=logging.getLogger("redshift_mcp.plugins"),
        sql_audit_logger=logging.getLogger("redshift_mcp.sql_audit"),
        request_id_var=contextvars.ContextVar("rid", default="-"),
        get_pool=lambda: (_ for _ in ()).throw(RuntimeError("连接池未初始化")),
        aexecute=db.aexecute,
    )


_SELECT = (
    "SELECT country, count(*) AS n FROM analytics.events "
    "WHERE event_date = %(date)s AND country = %(country)s GROUP BY country LIMIT 100"
)


def _tools(name: str = "top", **over) -> list[dict]:
    spec = {
        "name": name,
        "description": "按日期+国家统计",
        "sql": _SELECT,
        "params": [
            {"name": "date", "type": "date", "description": "US 日期"},
            {"name": "country", "type": "enum", "enum": ["US", "CA"], "description": "国家码"},
        ],
    }
    spec.update(over)
    return [spec]


def _fn(ctx: PluginContext, name: str):
    return ctx.mcp._tool_manager._tools[name].fn


def test_register_and_input_schema() -> None:
    ctx = _ctx(_tools())
    registered = register_sql_tools(ctx)
    assert registered == ["top"]
    tool = ctx.mcp._tool_manager._tools["top"]
    props = tool.parameters["properties"]
    assert props["country"]["enum"] == ["US", "CA"]
    assert props["date"]["type"] == "string"
    assert "US 日期" in props["date"]["description"]
    assert set(tool.parameters["required"]) == {"date", "country"}


async def test_valid_call_binds_named_params(monkeypatch) -> None:
    captured = {}

    def fake_execute(sql, params=None, *, max_rows, source=None):
        captured.update(sql=sql, params=params, max_rows=max_rows)
        return {"count": 0, "truncated": False, "columns": [], "rows": []}

    monkeypatch.setattr(db, "execute", fake_execute)
    ctx = _ctx(_tools(), max_rows=777)
    register_sql_tools(ctx)
    result = await _fn(ctx, "top")(date="2026-05-20", country="US")
    assert result["count"] == 0
    assert captured["params"] == {"date": "2026-05-20", "country": "US"}
    assert captured["max_rows"] == 777          # 用全局 query.max_rows
    assert captured["sql"] == _SELECT            # 执行用原始带占位符 SQL


async def test_max_rows_override(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(db, "execute",
                        lambda sql, params=None, *, max_rows, source=None: captured.update(max_rows=max_rows) or
                        {"count": 0, "truncated": False, "columns": [], "rows": []})
    ctx = _ctx(_tools(max_rows=5), max_rows=100)
    register_sql_tools(ctx)
    await _fn(ctx, "top")(date="2026-05-20", country="US")
    assert captured["max_rows"] == 5             # spec.max_rows 覆盖全局


async def test_bad_date_raises_valueerror(monkeypatch) -> None:
    monkeypatch.setattr(db, "execute", lambda *a, **k: pytest.fail("不该走到 db.execute"))
    ctx = _ctx(_tools())
    register_sql_tools(ctx)
    with pytest.raises(ValueError) as exc:
        await _fn(ctx, "top")(date="2026/05/20", country="US")
    assert "日期格式不合法" in str(exc.value)


async def test_db_error_wrapped_with_rid(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("pool down")

    monkeypatch.setattr(db, "execute", boom)
    ctx = _ctx(_tools())
    register_sql_tools(ctx)
    with pytest.raises(RuntimeError) as exc:
        await _fn(ctx, "top")(date="2026-05-20", country="US")
    msg = str(exc.value)
    # 直接调工具函数（绕过 FastMCP 的 "Error executing tool <name>:" 前缀）→ 看到裸 RuntimeError，
    # operation 用中性默认「查询」，消息形如 "查询 失败 (request_id=..., 详见服务端日志): RuntimeError"。
    # 不应再泄漏内部 source 前缀 "sql_tools:"（客户端侧的工具名由 FastMCP 前缀提供）。
    assert "查询 失败" in msg and "request_id=" in msg
    assert "sql_tools:" not in msg
    assert "日期格式不合法" not in msg


def test_duplicate_name_skipped(caplog) -> None:
    ctx = _ctx(_tools(name="dup"))

    @ctx.mcp.tool()
    def dup(x: int) -> dict:  # 预先占用 "dup" 这个工具名
        """preexisting"""
        return {"x": x}

    with caplog.at_level(logging.WARNING, logger="redshift_mcp.plugins.sql_tools"):
        registered = register_sql_tools(ctx)
    assert registered == []                       # 重名被跳过、不覆盖
    assert "dup" in caplog.text


async def test_optional_int_enum_params_are_nullable(monkeypatch) -> None:
    """L5：可选 int / enum 参数注解包成 Optional —— schema 标记可空、不在 required，
    省略调用时绑定 None 而不被签名/pydantic 拒。"""
    captured = {}
    monkeypatch.setattr(
        db, "execute",
        lambda sql, params=None, *, max_rows, source=None: captured.update(params=params) or
        {"count": 0, "truncated": False, "columns": [], "rows": []},
    )
    tools = [{
        "name": "opt",
        "description": "可选参数",
        "sql": "SELECT %(n)s AS n, %(c)s AS c LIMIT 1",
        "params": [
            {"name": "n", "type": "int", "required": False},
            {"name": "c", "type": "enum", "enum": ["US", "CA"], "required": False},
        ],
    }]
    ctx = _ctx(tools)
    assert register_sql_tools(ctx) == ["opt"]
    tool = ctx.mcp._tool_manager._tools["opt"]
    assert tool.parameters.get("required", []) == []        # 两个都可选
    # Optional 注解让 schema 允许 null（anyOf 含 null，或 type 列表含 "null"）
    n_schema = tool.parameters["properties"]["n"]
    assert "null" in str(n_schema)
    # 省略可选参数调用 → 绑定 None，不抛
    await _fn(ctx, "opt")()
    assert captured["params"] == {"n": None, "c": None}


async def test_missing_limit_auto_appended(monkeypatch, caplog) -> None:
    """safe=True 且顶层无 LIMIT → 自动追加 LIMIT (effective_max+1) 下推、记 info。"""
    captured = {}
    monkeypatch.setattr(db, "execute",
                        lambda sql, params=None, *, max_rows, source=None: captured.update(sql=sql, max_rows=max_rows) or
                        {"count": 0, "truncated": False, "columns": [], "rows": []})
    tools = [{
        "name": "nolimit",
        "description": "无 LIMIT",
        "sql": "SELECT country FROM analytics.events WHERE country = %(c)s",
        "params": [{"name": "c", "type": "string"}],
    }]
    ctx = _ctx(tools, max_rows=200)
    with caplog.at_level(logging.INFO, logger="redshift_mcp.plugins.sql_tools"):
        registered = register_sql_tools(ctx)
    assert registered == ["nolimit"]                        # 仍注册成功
    assert "自动追加 LIMIT 201" in caplog.text               # effective_max + 1
    await _fn(ctx, "nolimit")(c="US")
    # 执行 SQL 末尾追加了 LIMIT 201（占位符 %(c)s 原样保留，未被规整）
    assert captured["sql"].endswith("\nLIMIT 201")
    assert "%(c)s" in captured["sql"]
    assert captured["max_rows"] == 200


async def test_explicit_limit_respected(monkeypatch) -> None:
    """显式写了 LIMIT → 执行 SQL 原样不变（不追加、不收紧）。"""
    captured = {}
    monkeypatch.setattr(db, "execute",
                        lambda sql, params=None, *, max_rows, source=None: captured.update(sql=sql) or
                        {"count": 0, "truncated": False, "columns": [], "rows": []})
    ctx = _ctx(_tools(), max_rows=5)            # max_rows 远小于 SQL 里的 LIMIT 100
    register_sql_tools(ctx)
    await _fn(ctx, "top")(date="2026-05-20", country="US")
    assert captured["sql"] == _SELECT           # 原样尊重，不收紧到 LIMIT 6


async def test_auto_limit_respects_max_rows_override(monkeypatch) -> None:
    """自动追加的 LIMIT 用 spec.max_rows 覆盖后的有效上限。"""
    captured = {}
    monkeypatch.setattr(db, "execute",
                        lambda sql, params=None, *, max_rows, source=None: captured.update(sql=sql) or
                        {"count": 0, "truncated": False, "columns": [], "rows": []})
    tools = [{
        "name": "nolimit",
        "description": "无 LIMIT",
        "sql": "SELECT country FROM analytics.events WHERE country = %(c)s",
        "params": [{"name": "c", "type": "string"}],
        "max_rows": 5,
    }]
    ctx = _ctx(tools, max_rows=100)             # 全局 100，spec 覆盖为 5
    register_sql_tools(ctx)
    await _fn(ctx, "nolimit")(c="US")
    assert captured["sql"].endswith("\nLIMIT 6")    # spec.max_rows(5) + 1


def test_auto_appended_sql_stays_valid() -> None:
    """尾部带 ;/行注释的无 LIMIT SQL，追加后仍是合法单条只读 SELECT。"""
    from redshift_mcp import sql_guard
    from redshift_mcp.sql_tools import _PLACEHOLDER, _append_limit
    raw = "SELECT country FROM analytics.events WHERE country = %(c)s  -- 尾注释\n;"
    appended = _append_limit(raw, 101)
    # _append_limit 去掉了结尾 ; 并另起一行追加 LIMIT；占位符替换成字面量后能被闸门解析
    ast = sql_guard.assert_read_only(_PLACEHOLDER.sub("1", appended))
    assert ast.args.get("limit") is not None        # LIMIT 已生效在顶层


def test_optional_param_after_required_ok() -> None:
    # config 里 optional 在前、required 在后 —— 注册时自动排序，不报 Signature 错
    tools = [{
        "name": "ordering",
        "description": "顺序测试",
        "sql": "SELECT %(a)s AS a, %(b)s AS b",
        "params": [
            {"name": "a", "type": "string", "required": False, "default": "x"},
            {"name": "b", "type": "string", "required": True},
        ],
    }]
    ctx = _ctx(tools)
    assert register_sql_tools(ctx) == ["ordering"]
    assert set(ctx.mcp._tool_manager._tools["ordering"].parameters["required"]) == {"b"}


# ---- 安全闸门 ----


@pytest.mark.parametrize(
    "bad_sql",
    [
        "DELETE FROM analytics.t WHERE event_date = %(date)s",
        "DROP TABLE analytics.t",
        "SELECT 1; SELECT 2",                       # 多语句
        "SELECT * INTO tmp FROM analytics.t",             # SELECT INTO
        "UPDATE analytics.t SET x = 1",
    ],
)
def test_safe_gate_rejects_non_readonly(bad_sql, caplog) -> None:
    tools = [{"name": "danger", "description": "x", "sql": bad_sql,
              "params": [{"name": "date", "type": "date"}] if "%(date)s" in bad_sql else []}]
    ctx = _ctx(tools)
    with caplog.at_level(logging.ERROR, logger="redshift_mcp.plugins.sql_tools"):
        registered = register_sql_tools(ctx)
    assert registered == []                         # 默认 safe=True → 危险 SQL 被跳过
    assert "danger" not in ctx.mcp._tool_manager._tools
    assert "danger" in caplog.text


def test_safe_false_bypasses_gate() -> None:
    tools = [{"name": "danger", "description": "x", "safe": False,
              "sql": "DELETE FROM analytics.t WHERE event_date = %(date)s",
              "params": [{"name": "date", "type": "date"}]}]
    ctx = _ctx(tools)
    assert register_sql_tools(ctx) == ["danger"]    # safe=false → 闸门跳过，照常注册
    assert "danger" in ctx.mcp._tool_manager._tools
