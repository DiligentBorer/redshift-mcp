"""redshift-mcp 插件：query_error_api_by_date 工具。

这是「如何写一个 redshift-mcp 插件」的参考实现：
1. 在 ``pyproject.toml`` 声明 entry-point
   ``[project.entry-points."redshift_mcp.plugins"] complex = "redshift_mcp_complex:register"``；
2. 暴露一个 ``register(ctx: PluginContext) -> None`` 入口；
3. 在 ``register`` 内用 ``ctx.mcp.tool()`` 注册工具，闭包捕获 ``ctx`` 拿共享的
   连接池 / config / logger / request_id 等资源；DB 执行首选 ``ctx.aexecute``、异常包装用
   ``ctx.db_errors``，插件自身名字一律取 ``ctx.plugin_name`` 而非硬编码。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from redshift_mcp.plugin import PluginContext

from ._config import load_resolved_sql


def register(ctx: PluginContext) -> None:
    """插件注册入口：把 query_error_api_by_date 工具挂到宿主的 FastMCP 实例上。"""
    # 子 logger 名取 ctx.plugin_name（entry-point 名），避免硬编码插件自名。
    log = ctx.logger.getChild(ctx.plugin_name)
    # 启动时解析一次插件自有配置里的 SQL（缺配置则抛错，由 load_plugins 隔离、跳过本插件）。
    sql = load_resolved_sql(log, ctx.plugin_name)

    @ctx.mcp.tool()
    async def query_error_api_by_date(date: str) -> dict[str, Any]:
        """查询指定日期（US 时区）的 Error API IP 命中统计。

        Args:
            date: 日期字符串，格式 YYYY-MM-DD（如 "2026-05-20"）。

        Returns:
            字典，含以下字段：
              - count: 返回行数
              - truncated: 是否达到 max_rows 上限
              - columns: 列名列表
              - rows: {client_ip, device_count} 列表，按 device_count 降序
        """
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError as exc:
            # 入参校验错误 —— 直接回显给客户端，无需 rid。
            raise ValueError(
                f"日期格式不合法: {date!r}，期望 YYYY-MM-DD。"
            ) from exc

        # DB 执行复用 host 的 db.aexecute（经 ctx.aexecute，内部已做 to_thread / 计时 /
        # 行截断 / 审计）；DB 异常包装复用 ctx.db_errors（自动注入 rid + db_runtime_errors，
        # 不吞编程错误）。operation 用默认即可——客户端错误里的工具名由 FastMCP 前缀提供；
        # source 用 plugin:<ep.name>，进完成日志 / 审计便于按来源 grep。
        source = f"plugin:{ctx.plugin_name}"
        async with ctx.db_errors(logger=log):
            return await ctx.aexecute(
                sql,
                {"event_date": date, "limit": ctx.config.query.max_rows + 1},
                max_rows=ctx.config.query.max_rows,
                source=source,
            )
