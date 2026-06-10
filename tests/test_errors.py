"""测试 errors.db_errors_as_client_error 异常包装控制流 —— 全离线。"""
from __future__ import annotations

import logging

import pytest

from redshift_mcp.errors import db_errors_as_client_error

_LOG = logging.getLogger("redshift_mcp.tests.errors")


async def test_wraps_db_runtime_error_with_rid() -> None:
    with pytest.raises(RuntimeError) as exc:
        async with db_errors_as_client_error(
            logger=_LOG, operation="run_sql", rid="abc12345"
        ):
            raise ConnectionError("pool down")
    msg = str(exc.value)
    assert "run_sql 失败" in msg
    assert "request_id=abc12345" in msg
    assert "ConnectionError" in msg           # 暴露异常类名、不泄漏堆栈/SQL
    assert "pool down" not in msg             # 原始消息不外泄给客户端


async def test_does_not_swallow_programming_errors() -> None:
    # KeyError / TypeError 等不在 DB_RUNTIME_ERRORS 内 → 原样冒泡，便于早暴露 bug。
    with pytest.raises(KeyError):
        async with db_errors_as_client_error(logger=_LOG, operation="op", rid="r"):
            raise KeyError("a bug")


async def test_passthrough_when_no_error() -> None:
    ran: list[int] = []
    async with db_errors_as_client_error(logger=_LOG, operation="op", rid="r"):
        ran.append(1)
    assert ran == [1]


async def test_custom_db_errors_tuple() -> None:
    class MyErr(Exception):
        pass

    # 在自定义元组内 → 被包成 RuntimeError
    with pytest.raises(RuntimeError):
        async with db_errors_as_client_error(
            logger=_LOG, operation="op", rid="r", db_errors=(MyErr,)
        ):
            raise MyErr("x")

    # 不在元组内 → 原样冒泡
    with pytest.raises(ValueError):
        async with db_errors_as_client_error(
            logger=_LOG, operation="op", rid="r", db_errors=(MyErr,)
        ):
            raise ValueError("not wrapped")
