"""用 Starlette 的 TestClient 对 BearerAuthMiddleware 做端到端验证。"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from redshift_mcp.middleware import BearerAuthMiddleware, RequestIdMiddleware

TOKEN = "test-token-xyz"


def _make_app() -> Starlette:
    async def protected(request):
        return PlainTextResponse("ok")

    async def open_endpoint(request):
        return PlainTextResponse("open")

    app = Starlette(
        routes=[
            Route("/redshift", protected, methods=["GET", "POST"]),
            Route("/redshift/sub", protected, methods=["GET"]),
            Route("/healthz", open_endpoint, methods=["GET"]),
        ]
    )
    # add_middleware：最后加的处于最外层。这里顺序刻意与 server.py 一致：
    # auth 在内层、request-id 在外层。
    app.add_middleware(
        BearerAuthMiddleware, token=TOKEN, protected_path="/redshift"
    )
    app.add_middleware(RequestIdMiddleware)
    return app


def test_missing_auth_header_returns_401() -> None:
    client = TestClient(_make_app())
    resp = client.get("/redshift")
    assert resp.status_code == 401
    assert resp.json() == {"error": "缺少或格式错误的 Authorization 头"}
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_non_bearer_scheme_returns_401() -> None:
    client = TestClient(_make_app())
    resp = client.get("/redshift", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401


def test_wrong_token_returns_401() -> None:
    client = TestClient(_make_app())
    resp = client.get("/redshift", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401
    assert resp.json() == {"error": "token 无效"}


def test_correct_token_returns_200() -> None:
    client = TestClient(_make_app())
    resp = client.get("/redshift", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_subpath_under_protected_path_is_also_protected() -> None:
    client = TestClient(_make_app())
    resp_no = client.get("/redshift/sub")
    assert resp_no.status_code == 401
    resp_ok = client.get("/redshift/sub", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp_ok.status_code == 200


def test_unprotected_path_skips_auth() -> None:
    client = TestClient(_make_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "open"


def test_x_request_id_header_is_echoed_back() -> None:
    """RequestIdMiddleware 应该给每个响应都附上 X-Request-ID 头，
    包括 401（因为它包在 auth 外层）。"""
    client = TestClient(_make_app())
    # 401 路径也应携带 request id
    resp = client.get("/redshift")
    assert resp.status_code == 401
    assert "x-request-id" in {k.lower() for k in resp.headers.keys()}
    assert len(resp.headers["X-Request-ID"]) == 8  # 4 字节 hex


def test_x_request_id_passthrough_when_client_sends_one() -> None:
    client = TestClient(_make_app())
    resp = client.get(
        "/redshift",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Request-ID": "deadbeef",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "deadbeef"
