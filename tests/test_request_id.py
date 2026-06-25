"""验证 _install_per_request_context:每条请求处理入口按「发起请求」设 rid（消除会话级 rid）。

包一层 lowlevel server 的 ``_handle_request``,从 message 的 request_context.scope 取回
``RequestIdMiddleware`` 存入的 rid,在原方法执行期间设好 ``request_id_var``、结束复位。
全程离线,不连 DB / 不起真 server。
"""
from __future__ import annotations

import logging
import types

from redshift_mcp.middleware import (
    _SCOPE_RID_KEY,
    _SCOPE_SID_KEY,
    request_id_var,
    session_id_var,
)
from redshift_mcp.server import _install_per_request_context


class _FakeServer:
    """最小桩:一个 async _handle_request,记录执行时刻 request_id_var / session_id_var 的值。"""

    def __init__(self) -> None:
        self.captured: list[str] = []
        self.captured_sid: list[str] = []

    async def _handle_request(self, message, *args, **kwargs):
        self.captured.append(request_id_var.get())
        self.captured_sid.append(session_id_var.get())
        return "ok"


def _msg(scope_rid: str | None, scope_sid: str | None = None):
    """构造带 message_metadata.request_context.scope 的假消息。"""
    scope: dict[str, str] = {}
    if scope_rid is not None:
        scope[_SCOPE_RID_KEY] = scope_rid
    if scope_sid is not None:
        scope[_SCOPE_SID_KEY] = scope_sid
    rc = types.SimpleNamespace(scope=scope)
    return types.SimpleNamespace(message_metadata=types.SimpleNamespace(request_context=rc))


async def test_wrap_sets_per_request_rid_and_resets() -> None:
    server = _FakeServer()
    _install_per_request_context(server)
    result = await server._handle_request(_msg("req-abc"))
    assert result == "ok"                    # 原方法照常执行、返回透传
    assert server.captured == ["req-abc"]    # 执行期间 rid = 发起请求的 rid
    assert request_id_var.get() == "-"       # 调用后复位为默认


async def test_wrap_no_metadata_leaves_var_untouched() -> None:
    server = _FakeServer()
    _install_per_request_context(server)
    token = request_id_var.set("session-base")
    try:
        # message_metadata 为 None → 取不到 → 不改 request_id_var
        await server._handle_request(types.SimpleNamespace(message_metadata=None))
        assert server.captured == ["session-base"]
    finally:
        request_id_var.reset(token)


async def test_wrap_scope_without_key_leaves_var_untouched() -> None:
    server = _FakeServer()
    _install_per_request_context(server)
    token = request_id_var.set("session-base2")
    try:
        await server._handle_request(_msg(None))  # scope 存在但无该键
        assert server.captured == ["session-base2"]
    finally:
        request_id_var.reset(token)


def test_install_warns_when_no_handle_request(caplog) -> None:
    class _Bare:
        pass

    bare = _Bare()
    with caplog.at_level(logging.WARNING, logger="redshift_mcp"):
        _install_per_request_context(bare)
    assert "未安装 per-request rid 包装" in caplog.text
    assert not hasattr(bare, "_handle_request")  # 未误装属性


async def test_wrap_sets_per_request_rid_and_sid_together() -> None:
    """rid 与 sid 由包装从 scope 一并取回、同设同复位（成对保证在处理侧的体现）。"""
    server = _FakeServer()
    _install_per_request_context(server)
    result = await server._handle_request(_msg("req-abc", "sess-xyz"))
    assert result == "ok"
    assert server.captured == ["req-abc"]
    assert server.captured_sid == ["sess-xyz"]
    assert request_id_var.get() == "-"
    assert session_id_var.get() == "-"  # 两者调用后均复位
