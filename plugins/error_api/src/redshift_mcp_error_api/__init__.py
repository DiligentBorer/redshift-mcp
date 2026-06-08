"""redshift-mcp 插件：query_error_api_by_date 工具。

这是「如何写一个 redshift-mcp 插件」的参考实现：
1. 在 ``pyproject.toml`` 声明 entry-point
   ``[project.entry-points."redshift_mcp.plugins"] error_api = "redshift_mcp_error_api:register"``；
2. 暴露一个 ``register(ctx: PluginContext) -> None`` 入口；
3. 在 ``register`` 内用 ``ctx.mcp.tool()`` 注册工具，闭包捕获 ``ctx`` 拿共享的
   连接池 / config / logger / request_id 等资源。
"""
from __future__ import annotations

from datetime import datetime
from functools import partial
from typing import Any

import anyio
from redshift_mcp.plugin import PluginContext

from ._config import load_resolved_sql
from .query import run_query


def register(ctx: PluginContext) -> None:
    """插件注册入口：把 query_error_api_by_date 工具挂到宿主的 FastMCP 实例上。"""
    # 用 redshift_mcp.plugins.error_api 子 logger（自动冒泡到主 handler）。
    log = ctx.logger.getChild("error_api")
    # 启动时解析一次插件自有配置里的 SQL（缺配置则抛错，由 load_plugins 隔离、跳过本插件）。
    sql = load_resolved_sql(log)

    @ctx.mcp.tool()
    async def query_error_api_by_date(date: str) -> dict[str, Any]:
        """查询指定日期（US 时区）的 Error API IP 命中统计。

        Args:
            date: 日期字符串，格式 YYYY-MM-DD（如 "2026-05-20"）。

        Returns:
            字典，含以下字段：
              - date: 入参日期
              - count: 返回行数
              - truncated: 是否达到 max_rows 上限
              - rows: {client_ip, device_count} 列表，按 device_count 降序
        """
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            # 入参校验错误 —— 直接回显给客户端，无需 rid。
            raise ValueError(
                f"日期格式不合法: {date!r}，期望 YYYY-MM-DD。"
            ) from exc

        rid = ctx.request_id_var.get()
        try:
            # 阻塞查询丢到 worker 线程，不阻塞事件循环。
            return await anyio.to_thread.run_sync(
                partial(
                    run_query,
                    ctx.get_pool(),
                    date,
                    sql=sql,
                    max_rows=ctx.config.query.max_rows,
                    logger=log,
                )
            )
        except ctx.db_runtime_errors as exc:
            # 完整 traceback（带 rid）通过 filter 写到服务端日志。
            log.exception("error_api 查询失败 date=%s", date)
            # 给客户端一条精简消息 + rid，运维侧可按 rid grep 日志。
            raise RuntimeError(
                f"查询失败 (request_id={rid}, 详见服务端日志): "
                f"{exc.__class__.__name__}"
            ) from exc
