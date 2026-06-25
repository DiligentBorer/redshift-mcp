"""HTTP 接入层：每请求 rid 基建 + 两个 ASGI 中间件 + initialize body 辅助。

从 ``server.py`` 抽出,集中承载「请求一进来就染 rid、鉴权、读 initialize 取 clientInfo」这套
HTTP/ASGI 关切。本模块**只依赖 stdlib / starlette / mcp,不 import ``server.py``** —— 因为
``request_id_var`` 被中间件 / 日志 filter / 工具 / ``PluginContext`` 共用,若留在 server 会与
「server import 本模块挂中间件」形成循环依赖,故 rid 基建随中间件一并放这里。

``server.py`` 从这里 import ``request_id_var`` / ``RequestIdMiddleware`` / ``BearerAuthMiddleware``;
``build_log_config`` 的 dictConfig 以 dotted-path ``redshift_mcp.middleware.RequestIdFilter`` 引用 filter。
"""
from __future__ import annotations

import contextvars
import hmac
import json
import logging
import re
import secrets

from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# 与 server 同名 logger 对象(用于「会话建立」一行);子 logger 命名空间一致,自然冒泡到主 handler。
logger = logging.getLogger("redshift_mcp")

# 每请求关联 id。由 RequestIdMiddleware 设置；非请求上下文中默认为 "-"。
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

# 会话关联 id（Mcp-Session-Id）。与 rid 同机制：中间件从请求头取 / initialize 从响应取，
# 经 scope 传给 _install_per_request_context 在会话 task 里取回。日志里截前 8 位显示（见 RequestIdFilter）。
session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_id", default="-"
)


def new_request_id() -> str:
    """生成 8 位 hex 字符串（~32 bit），单日志窗口内做 grep 关联足够。"""
    return secrets.token_hex(4)


# RequestIdMiddleware 把每个 HTTP 请求的 rid 同时塞进 ASGI scope[_SCOPE_RID_KEY]。
# MCP 会话消息处理跑在「建会话时起的长生命周期 task」里、其 request_id_var 恒为建会话那次
# 请求的 rid；server.py 的 _install_per_request_context 在每条消息处理入口经
# message.message_metadata.request_context.scope 取回本键，把 rid 重置为「发起该消息的请求」
# 的 rid —— 从而每个 HTTP 请求全程同 id、消除会话级 rid。
_SCOPE_RID_KEY = "redshift_mcp_request_id"
# 同理:每个 HTTP 请求的 session id 也存进 scope，供会话 task 里的包装取回。
_SCOPE_SID_KEY = "redshift_mcp_session_id"

# uuid4().hex 固定 32 位小写 hex。用于 filter 兜底:从 SDK 生命周期日志（Created new transport /
# Terminating session / Session X crashed/idle）的消息文本里抽出内嵌的 session id。
_SID_RE = re.compile(r"[0-9a-f]{32}")


class RequestIdFilter(logging.Filter):
    """把当前 request_id / session_id 注入到每条 LogRecord，便于 formatter 渲染。

    rid 与 sid **由本 filter 在每条记录上同时设置**、由同一格式串渲染，故两者永远成对出现。
    sid 渲染为前 8 位（窗口内唯一、且是 SDK 完整 id 行的前缀，可 grep 关联）。

    sid 兜底:initialize 那轮的 session id 由 SDK 中途 ``uuid4().hex`` 生成、只在它自己生命周期
    日志的消息里(此时 ``session_id_var`` 还是 "-"），故对 ``mcp.server.streamable_http*`` logger
    的记录,从消息文本里抽出内嵌的 32hex id —— 让 ``Created new transport`` 等行的 sid 也有值。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        sid = session_id_var.get()
        if sid == "-" and record.name.startswith("mcp.server.streamable_http"):
            match = _SID_RE.search(record.getMessage())
            if match:
                sid = match.group(0)
        record.session_id = sid[:8] if sid != "-" else "-"
        return True


async def _drain_body(receive) -> bytes:
    """收齐一个请求的 body。

    MCP ``initialize`` 是普通小 JSON POST，可安全全量缓冲。遇到非 ``http.request``
    事件（如 ``http.disconnect``）即停。
    """
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay_body(body: bytes, receive):
    """回放缓冲的 ``body`` 一次，之后把 receive **交还真实 receive**。

    关键：body 回放完后必须委托回真实 ``receive``（让它照常阻塞、透传真正的
    ``http.disconnect`` 等事件）；若在此伪造 ``http.disconnect``，流式（SSE）响应的
    「监听断开」任务会立刻收到假断开而取消整个响应 → ``No response returned``。
    """
    delivered = False

    async def _receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return _receive


def _extract_init_client(body: bytes) -> str | None:
    """从 JSON-RPC body 提取 ``initialize`` 的 clientInfo，拼成 ``name/version``。

    非 ``initialize`` / body 非 JSON / 结构异常一律返回 ``None``（调用方据此放行、不记日志）。
    字段缺失时降级为 ``unknown`` / ``?``。
    """
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("method") != "initialize":
        return None
    params = payload.get("params")
    info = (params or {}).get("clientInfo") if isinstance(params, dict) else None
    info = info if isinstance(info, dict) else {}
    name = info.get("name") or "unknown"
    version = info.get("version") or "?"
    return f"{name}/{version}"


class RequestIdMiddleware:
    """每请求 rid 染色 + 响应回写 ``X-Request-ID``；并在 MCP ``initialize`` 时补记一行
    ``会话建立 session=.. client=..``（同 rid / 同 session，便于按来源客户端定位连接）。

    纯 ASGI（非 BaseHTTPMiddleware）：取 clientInfo 需读 initialize **请求体**并原样回放给下游，
    而 BaseHTTPMiddleware 里读 body 会消耗 receive、下游（MCP）读不到，故用纯 ASGI 做
    「读 body → 回放」。rid 仍由本中间件管理：从上游 ``X-Request-ID`` 头取或新生成，set 进
    ``request_id_var`` 供下游 logger / 工具读取；挂在最外层，故 401 响应也带 ``X-Request-ID``。

    仅「无 session 头的 POST」（即 initialize，低频）才缓冲并解析 body；其余请求不碰 body、
    不影响流式响应。body 解析失败 / 非 initialize / 字段缺失一律静默降级（client=None，不记会话行）。
    """

    _HEADER = b"x-request-id"
    _SESSION_HEADER = MCP_SESSION_ID_HEADER.encode("latin-1")

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        rid = (headers.get(self._HEADER) or b"").decode("latin-1") or new_request_id()
        # 存进本请求 scope：会话 task 处理具体消息时，server 的包装经 SDK 的
        # request_ctx/message metadata 取回它，实现每请求独立 rid（消除会话级 rid）。
        scope[_SCOPE_RID_KEY] = rid
        # 会话 id：后续请求的请求头带 Mcp-Session-Id；initialize 请求头没有（"-"），其 sid 由
        # 下面 send_wrapper 从响应头补设。同样存进 scope，供会话 task 里的包装取回。
        sid_raw = headers.get(self._SESSION_HEADER)
        sid = sid_raw.decode("latin-1") if sid_raw else "-"
        scope[_SCOPE_SID_KEY] = sid

        client: str | None = None
        body: bytes | None = None
        if scope.get("method") == "POST" and self._SESSION_HEADER not in headers:
            body = await _drain_body(receive)
            client = _extract_init_client(body)

        token = request_id_var.set(rid)
        sid_token = session_id_var.set(sid)
        try:
            async def send_wrapper(message) -> None:
                if message["type"] == "http.response.start":
                    # 回写 X-Request-ID（去掉可能已存在的同名头再追加，避免重复）。
                    resp_headers = [
                        (k, v) for (k, v) in message.get("headers", [])
                        if k.lower() != self._HEADER
                    ]
                    resp_headers.append((self._HEADER, rid.encode("latin-1")))
                    message = {**message, "headers": resp_headers}
                    # initialize 请求补一行会话日志；响应带 Mcp-Session-Id 就填上，没有（如 401）→ "-"。
                    if client is not None:
                        resp_sid_raw = next(
                            (v for (k, v) in resp_headers if k == self._SESSION_HEADER),
                            None,
                        )
                        resp_sid = resp_sid_raw.decode("latin-1") if resp_sid_raw else "-"
                        # initialize 的 session id 此刻才从响应拿到：设入 var 使本行前缀 sid 也有值
                        # （完整 id 仍在下面 session= 字段）。
                        session_id_var.set(resp_sid)
                        logger.info(
                            "会话建立 session=%s client=%s",
                            resp_sid,
                            client,
                        )
                await send(message)

            downstream = _replay_body(body, receive) if body is not None else receive
            await self.app(scope, downstream, send_wrapper)
        finally:
            session_id_var.reset(sid_token)
            request_id_var.reset(token)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, token: str, protected_path: str) -> None:
        super().__init__(app)
        self._token = token
        self._protected_path = protected_path.rstrip("/")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        protected = (
            path == self._protected_path
            or path.startswith(self._protected_path + "/")
        )
        if not protected:
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        scheme, _, presented = auth.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return JSONResponse(
                {"error": "缺少或格式错误的 Authorization 头"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not hmac.compare_digest(presented, self._token):
            return JSONResponse(
                {"error": "token 无效"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
