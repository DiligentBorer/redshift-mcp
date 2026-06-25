"""验证 RequestIdMiddleware（纯 ASGI）在 MCP initialize 时补记会话/client 日志。

会话建立时打一行 ``会话建立 session=... client=...``，其余请求不记；并含 ``_replay_body``
的回归测试（body 回放后须交还真实 receive，否则流式响应被取消 → No response returned）。
全程离线，不连 DB / 不连真客户端。
"""
from __future__ import annotations

import logging

from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from redshift_mcp.middleware import RequestIdMiddleware, _replay_body

_SID = "test-session-abc123"


def _make_app() -> Starlette:
    async def endpoint(request):
        # 读 body 证明回放后下游仍拿到完整请求体；响应带 Mcp-Session-Id 模拟 SDK 下发会话 id。
        body = await request.body()
        return PlainTextResponse(body, headers={MCP_SESSION_ID_HEADER: _SID})

    app = Starlette(routes=[Route("/redshift", endpoint, methods=["POST"])])
    app.add_middleware(RequestIdMiddleware)
    return app


def _init_body(client_info: dict | None) -> dict:
    params: dict = {"protocolVersion": "2024-11-05", "capabilities": {}}
    if client_info is not None:
        params["clientInfo"] = client_info
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}


def test_initialize_logs_session_and_client(caplog) -> None:
    client = TestClient(_make_app())
    body = _init_body({"name": "Claude Desktop", "version": "1.2.3"})
    with caplog.at_level(logging.INFO, logger="redshift_mcp"):
        resp = client.post("/redshift", json=body)
    assert resp.status_code == 200
    assert resp.json() == body  # body 回放正确，下游完整收到
    assert f"会话建立 session={_SID} client=Claude Desktop/1.2.3" in caplog.text
    # rid 也回写到了响应头
    assert "x-request-id" in {k.lower() for k in resp.headers.keys()}


def test_initialize_missing_clientinfo_logs_unknown(caplog) -> None:
    client = TestClient(_make_app())
    with caplog.at_level(logging.INFO, logger="redshift_mcp"):
        resp = client.post("/redshift", json=_init_body(None))
    assert resp.status_code == 200
    assert f"会话建立 session={_SID} client=unknown/?" in caplog.text


def test_request_with_session_header_is_passthrough(caplog) -> None:
    """带 Mcp-Session-Id 头的后续请求 → 放行，不记会话日志、不碰 body。"""
    client = TestClient(_make_app())
    body = _init_body({"name": "X", "version": "1"})
    with caplog.at_level(logging.INFO, logger="redshift_mcp"):
        resp = client.post(
            "/redshift", json=body, headers={MCP_SESSION_ID_HEADER: "existing-sid"}
        )
    assert resp.status_code == 200
    assert resp.json() == body
    assert "会话建立" not in caplog.text


def test_non_initialize_post_not_logged(caplog) -> None:
    """非 initialize 的 POST（如 tools/call）→ 解析后放行，不记日志、body 完好。"""
    client = TestClient(_make_app())
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}}
    with caplog.at_level(logging.INFO, logger="redshift_mcp"):
        resp = client.post("/redshift", json=body)
    assert resp.status_code == 200
    assert resp.json() == body
    assert "会话建立" not in caplog.text


def test_non_json_post_not_logged_and_body_intact(caplog) -> None:
    """body 非 JSON → 静默放行，不报错、不记日志，下游仍拿到原始 body。"""
    client = TestClient(_make_app())
    with caplog.at_level(logging.INFO, logger="redshift_mcp"):
        resp = client.post("/redshift", content=b"not json at all")
    assert resp.status_code == 200
    assert resp.content == b"not json at all"
    assert "会话建立" not in caplog.text


async def test_replay_body_delegates_to_real_receive_after_body() -> None:
    """回归：_replay_body 回放 body 后，后续 receive 必须委托真实 receive。

    旧实现回放后伪造 http.disconnect，会让流式响应的「监听断开」任务收到假断开而取消整个
    响应（No response returned）。此测试让真实 receive 在 body 之后返回一个可辨识事件，
    断言第二次拿到的是它、而非伪造的 disconnect。
    """
    sentinel = {"type": "http.request", "body": b"NEXT", "more_body": False}
    calls: list[int] = []

    async def real_receive():
        calls.append(1)
        return sentinel

    recv = _replay_body(b'{"x":1}', real_receive)
    first = await recv()
    assert first == {"type": "http.request", "body": b'{"x":1}', "more_body": False}
    assert calls == []  # 回放 body 时没碰真实 receive

    second = await recv()
    assert second is sentinel  # 委托给了真实 receive（而非旧的伪造 http.disconnect）
    assert calls == [1]
