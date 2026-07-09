"""共享异常定义与错误处理控制流（redshift-mcp 契约层 SDK）。

这是一个**零依赖叶子模块**：只 import 标准库 +（可选）psycopg，不 import 任何 host 实现。
把 ``DB_RUNTIME_ERRORS`` 与 ``db_errors_as_client_error`` 放在 SDK 里，是为了让 host 与外部
插件复用同一组「DB / 运行时错误」分类与异常包装控制流，而**不必 import host 源码**。

psycopg 为**可选依赖**：装了则 ``DB_RUNTIME_ERRORS`` 含 ``psycopg.Error``；未装（如插件的隔离
dev venv）则守卫式降级为不含它的元组，``import`` 本模块不会失败。生产运行在 host venv 里 psycopg
必然存在，语义完整。
"""
from __future__ import annotations

import contextlib
import logging
from typing import AsyncIterator

try:
    import psycopg

    _PSYCOPG_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # 插件隔离 dev venv 未装 psycopg —— 守卫式降级，不让 import 失败
    _PSYCOPG_ERRORS = ()

# 工具 / 插件在调用 DB 的路径上，应把这一组「DB / 运行时错误」包装成带 rid 的
# RuntimeError 抛给客户端，但**不**吞掉编程错误（TypeError / KeyError / sqlglot
# 内部断言等）—— 让那些 bug 类异常原样冒泡，由 FastMCP 包成 500，便于早暴露。
DB_RUNTIME_ERRORS: tuple[type[BaseException], ...] = (
    *_PSYCOPG_ERRORS,
    RuntimeError,
    ConnectionError,
    TimeoutError,
)


@contextlib.asynccontextmanager
async def db_errors_as_client_error(
    *,
    logger: logging.Logger,
    operation: str,
    rid: str,
    db_errors: tuple[type[BaseException], ...] = DB_RUNTIME_ERRORS,
) -> AsyncIterator[None]:
    """async 上下文管理器：把 ``db_errors`` 范围内的异常包成带 rid 的 ``RuntimeError``。

    捕获到属于 ``db_errors`` 的异常时：记一条带完整 traceback 的 ``logger.exception``，再抛出
    面向客户端的精简 ``RuntimeError``（含 ``request_id`` 可追溯字段，不泄漏堆栈 / SQL / 凭证）；
    **不在 ``db_errors`` 内的异常（``TypeError`` / ``KeyError`` 等编程错误）原样冒泡**，便于早暴露。

    供插件经 ``PluginContext.db_errors`` 复用，免去每个插件重抄「取 rid → try/except → 包 rid」
    样板。host 内建工具（``run_sql`` / ``describe_table``）保留各自的 bespoke 处理，不走本 CM。
    """
    try:
        yield
    except db_errors as exc:
        logger.exception("%s 失败", operation)
        raise RuntimeError(
            f"{operation} 失败 (request_id={rid}, 详见服务端日志): "
            f"{exc.__class__.__name__}"
        ) from exc
