"""测试工具入口处的日期格式校验。

日期校验在任何数据库访问**之前**进行，所以不需要 mock 任何东西；
非法日期直接抛 ValueError。本测试构造一个最小 ``PluginContext``、调
``register`` 取出注册的工具函数，再直接调用它 —— 全程离线，不连 DB。
"""
from __future__ import annotations

import contextvars
import logging
from typing import Callable

import pytest

from redshift_mcp.config import AppConfig
from redshift_mcp.plugin import PluginContext
from redshift_mcp_error_api import register


@pytest.fixture(autouse=True)
def _error_api_config(tmp_path, monkeypatch):
    """register 现在要求插件自有配置 —— 喂一份最小 config（含内联 SQL）让它通过。

    日期校验与本配置无关，只是让 ``register`` 能成功解析 SQL 并注册工具。
    """
    cfg = tmp_path / "config.yaml"
    cfg.write_text('sql: "SELECT 1"\n', encoding="utf-8")
    monkeypatch.setenv("REDSHIFT_MCP_ERROR_API_CONFIG", str(cfg))


class _CapturingMCP:
    """最小 FastMCP 替身：``.tool()`` 装饰器只把被注册函数捕获下来供测试调用。"""

    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _build_tool(get_pool: Callable = lambda: (_ for _ in ()).throw(
    RuntimeError("连接池未初始化")
)) -> Callable:
    """构造 PluginContext 并 register，返回注册好的 query_error_api_by_date。

    默认 ``get_pool`` 一被调用就抛 RuntimeError，模拟「连接池未初始化」。
    """
    mcp = _CapturingMCP()
    cfg = AppConfig.model_validate(
        {
            "database": {"host": "h", "dbname": "d", "user": "u"},
            "server": {"auth_token": "t"},
            "query": {"max_rows": 100},
        }
    )
    ctx = PluginContext(
        mcp=mcp,
        config=cfg,
        logger=logging.getLogger("redshift_mcp.plugins"),
        sql_audit_logger=logging.getLogger("redshift_mcp.sql_audit"),
        request_id_var=contextvars.ContextVar("rid", default="-"),
        get_pool=get_pool,
    )
    register(ctx)
    return mcp.tools["query_error_api_by_date"]


@pytest.mark.parametrize(
    "bad_date",
    [
        "2026/05/20",     # 错误的分隔符
        "20260520",       # 无分隔符
        "2026-13-01",     # 月份越界
        "2026-02-30",     # 日越界
        "20-05-2026",     # 顺序错误
        "",               # 空字符串
        "not-a-date",
    ],
)
async def test_invalid_dates_rejected(bad_date: str) -> None:
    tool = _build_tool()
    with pytest.raises(ValueError) as excinfo:
        await tool(bad_date)
    msg = str(excinfo.value)
    assert "日期格式不合法" in msg
    assert "YYYY-MM-DD" in msg


async def test_valid_date_progresses_past_strptime() -> None:
    """对于格式正确的日期，strptime 不应抛错；之后流转到 DB 访问层。

    这里 get_pool 抛 RuntimeError（连接池未初始化），会被 db_runtime_errors
    捕获、包成带 rid 的 RuntimeError。断言它**不是**日期错误，正是要验证的
    边界 —— 入参校验对合法日期不会短路。
    """
    tool = _build_tool()
    with pytest.raises(RuntimeError) as excinfo:
        await tool("2026-05-20")
    msg = str(excinfo.value)
    assert "日期格式不合法" not in msg
    assert "查询失败" in msg
