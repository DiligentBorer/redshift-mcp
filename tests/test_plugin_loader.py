"""插件框架测试：entry_points 加载器 + PluginContext 契约。

全程离线：monkeypatch ``redshift_mcp.plugin.entry_points`` 返回伪 EntryPoint，
不真安装任何包；用独立的 ``FastMCP("test")`` 实例验证工具注册，避免污染
``server.mcp`` 全局。
"""
from __future__ import annotations

import contextvars
import dataclasses
import logging
from typing import Callable

import pytest
from mcp.server.fastmcp import FastMCP

from redshift_mcp import db, plugin
from redshift_mcp.config import AppConfig
from redshift_mcp.errors import DB_RUNTIME_ERRORS
from redshift_mcp.plugin import PluginContext, load_plugins


class _FakeEP:
    """伪 importlib.metadata.EntryPoint：只实现 load 器需要的 name/value/load()。"""

    def __init__(self, name: str, value: str, loader: Callable[[], object]) -> None:
        self.name = name
        self.value = value
        self._loader = loader

    def load(self) -> object:
        return self._loader()


def _pool_unavailable():
    raise RuntimeError("连接池未初始化")


def _build_ctx() -> PluginContext:
    cfg = AppConfig.model_validate(
        {
            "database": {"host": "h", "dbname": "d", "user": "u"},
            "server": {"auth_token": "t"},
            "query": {"max_rows": 100},
        }
    )
    return PluginContext(
        mcp=FastMCP("test"),
        config=cfg,
        logger=logging.getLogger("redshift_mcp.plugins"),
        sql_audit_logger=logging.getLogger("redshift_mcp.sql_audit"),
        request_id_var=contextvars.ContextVar("rid", default="-"),
        get_pool=_pool_unavailable,
    )


def _patch_eps(monkeypatch, eps: list[_FakeEP]) -> None:
    """让 load_plugins 看到的 entry_points(group=...) 返回给定的伪 EP 列表。"""
    monkeypatch.setattr(plugin, "entry_points", lambda group=None: list(eps))


# ---- 用于注册的 register 函数（具名 + 带注解，FastMCP 才能推断 schema）----


def _good_register(ctx: PluginContext) -> None:
    @ctx.mcp.tool()
    def good_tool(x: int) -> int:
        """一个用于测试的工具。"""
        return x


def _raising_register(ctx: PluginContext) -> None:
    raise RuntimeError("register 内部炸了")


def _bad_loader():
    raise ImportError("插件 import 失败")


def _tool_names(ctx: PluginContext) -> list[str]:
    return list(ctx.mcp._tool_manager._tools.keys())


def test_discovers_and_registers(monkeypatch) -> None:
    ctx = _build_ctx()
    _patch_eps(monkeypatch, [_FakeEP("good", "pkg:register", lambda: _good_register)])

    loaded = load_plugins(ctx)

    assert loaded == ["good"]
    assert "good_tool" in _tool_names(ctx)


def test_bad_plugins_isolated(monkeypatch, caplog) -> None:
    ctx = _build_ctx()
    _patch_eps(
        monkeypatch,
        [
            _FakeEP("bad_load", "p:r", _bad_loader),                 # load() 抛错
            _FakeEP("bad_register", "p:r", lambda: _raising_register),  # register 抛错
            _FakeEP("not_callable", "p:obj", lambda: object()),      # load 返回非 callable
            _FakeEP("good", "p:r", lambda: _good_register),          # 正常
        ],
    )

    with caplog.at_level(logging.DEBUG, logger="redshift_mcp.plugins"):
        loaded = load_plugins(ctx)

    # 只有 good 成功；坏插件全被隔离跳过，不影响 good 注册
    assert loaded == ["good"]
    assert "good_tool" in _tool_names(ctx)
    # 三个坏插件各自记了日志（按名字出现在日志里）
    text = caplog.text
    for bad in ("bad_load", "bad_register", "not_callable"):
        assert bad in text


def test_disabled_plugin_skipped(monkeypatch) -> None:
    ctx = _build_ctx()
    _patch_eps(monkeypatch, [_FakeEP("good", "p:r", lambda: _good_register)])

    loaded = load_plugins(ctx, disabled=["good"])

    assert loaded == []
    assert "good_tool" not in _tool_names(ctx)


def test_empty_discovery_is_noop(monkeypatch) -> None:
    ctx = _build_ctx()
    _patch_eps(monkeypatch, [])

    assert load_plugins(ctx) == []


def test_plugin_context_is_frozen_with_default_errors() -> None:
    ctx = _build_ctx()
    assert ctx.db_runtime_errors is DB_RUNTIME_ERRORS
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.config = None  # type: ignore[misc]


def test_get_pool_uninitialized_raises(monkeypatch) -> None:
    monkeypatch.setattr(db, "_pool", None)
    with pytest.raises(RuntimeError, match="连接池未初始化"):
        db.get_pool()
