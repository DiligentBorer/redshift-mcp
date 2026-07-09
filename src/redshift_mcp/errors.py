"""共享异常定义（back-compat re-export）。

``DB_RUNTIME_ERRORS`` 与 ``db_errors_as_client_error`` 已抽到契约层 SDK
``redshift_mcp_sdk.errors``（让 host 与外部插件共享同一份、插件不必 import host 源码）。
host 从那里**别名 re-export**，保证既有 ``from redshift_mcp.errors import ...`` 与「同一对象」
身份断言（如 ``ctx.db_runtime_errors is DB_RUNTIME_ERRORS``）不变。
"""
from __future__ import annotations

from redshift_mcp_sdk.errors import DB_RUNTIME_ERRORS, db_errors_as_client_error

__all__ = ["DB_RUNTIME_ERRORS", "db_errors_as_client_error"]
