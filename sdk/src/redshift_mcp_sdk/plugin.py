"""插件框架契约：宿主→插件的 ``PluginContext`` + entry-point group 常量。

本模块是 redshift-mcp 插件的**稳定公开契约**。外部插件只 import 本包即可编写 / 类型检查 /
离线单测，**不必引入 host（redshift-mcp）的实现源码**。

分发模型为「wheel + entry_points 安装式」：插件是独立的可安装包，在自己的
``pyproject.toml`` 里声明

    [project.entry-points."redshift_mcp.plugins"]
    <name> = "<import_pkg>:register"

装进与主程序同一个 venv 后，host 的加载器（``redshift_mcp.plugin.load_plugins``）通过
``importlib.metadata.entry_points(group="redshift_mcp.plugins")`` 自动发现，逐个调用插件的
``register(ctx)``。插件在 ``register`` 内用 ``ctx.mcp.tool()`` 注册工具，并通过闭包捕获 ``ctx``
拿到共享的连接池 / config / logger 等资源。

为保持 SDK「薄」，``config`` / ``mcp`` 字段用 **Protocol** 收窄（``AppConfigLike`` /
``McpLike``），只暴露插件实际会用到的最小接口 —— 这样 SDK 无需依赖 host 的 ``config.py``
或 ``mcp`` 包；host 在构造 ``PluginContext`` 时传入真实的 ``AppConfig`` / ``FastMCP``，结构上
满足即可（``from __future__ import annotations`` 下注解为字符串，dataclass 运行时不 eval，
零风险）。
"""
from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from .errors import DB_RUNTIME_ERRORS, db_errors_as_client_error

if TYPE_CHECKING:  # 仅类型注解用；运行时不 import（保持 SDK 不硬依赖 psycopg_pool）
    from psycopg_pool import ConnectionPool

# 插件 entry-point 的 group 名。插件 pyproject.toml 里要写成完全一致的字符串（直接写死，不 import）。
GROUP = "redshift_mcp.plugins"


class QueryConfigLike(Protocol):
    """``ctx.config.query`` 的最小契约面（插件实际会读的查询相关配置）。"""

    max_rows: int
    statement_timeout_ms: int
    timezone: str


class AppConfigLike(Protocol):
    """``ctx.config`` 的最小契约面。只承诺 ``query`` 子配置进稳定契约。

    host 的真实 ``AppConfig`` 还有 ``database`` / ``server`` / ``tables`` 等字段，但那些不进
    插件契约（运行时能拿到，类型上不承诺）—— 这是刻意的收窄。
    """

    query: QueryConfigLike


class McpLike(Protocol):
    """``ctx.mcp`` 的最小契约面：插件用 ``tool()`` 装饰器或 ``add_tool()`` 注册工具。

    签名用宽松的 ``*args, **kwargs``，避免 FastMCP 升级改签名把契约拖挂。
    """

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

    def add_tool(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class PluginContext:
    """宿主注入给每个插件的共享上下文（**只读契约 / 公开 API，需保持稳定**）。

    破坏性修改本类字段会让已安装的插件失配，必须升 SDK 主版本并在 CHANGELOG 标注。
    插件应只读使用本对象，不要修改其中的 ``config`` 等可变成员。
    """

    mcp: McpLike
    """FastMCP 实例（结构上满足 ``McpLike``）。插件用 ``ctx.mcp.tool()`` 装饰器（或 ``add_tool``）注册工具。"""

    config: AppConfigLike
    """应用配置（结构上满足 ``AppConfigLike``）。插件常用 ``ctx.config.query.max_rows`` 等。"""

    logger: logging.Logger
    """``redshift_mcp.plugins`` logger。插件建议用 ``ctx.logger.getChild("<name>")``。"""

    sql_audit_logger: logging.Logger
    """SQL 审计专用 logger（``redshift_mcp.sql_audit``），需要审计 SQL 文本的插件用。"""

    request_id_var: contextvars.ContextVar[str]
    """每请求关联 id 的 ContextVar（与 server.py 同一个对象）。在工具函数体内
    ``.get()`` 读到当前请求的 rid，用于面向客户端错误消息的可追溯字段。"""

    get_pool: Callable[[], "ConnectionPool"]
    """返回共享连接池的 callable（注入 ``db.get_pool``）；未初始化时抛 RuntimeError。"""

    aexecute: Callable[..., Awaitable[dict[str, Any]]]
    """高层参数化 SELECT 执行入口（注入 ``db.aexecute``）。插件**首选**用它跑只读查询：
    ``await ctx.aexecute(sql, {bind...}, max_rows=..., source=f"plugin:{ctx.plugin_name}")``，
    内部已做 to_thread / 计时 / 行截断 / 审计，免去插件自管连接池。返回
    ``{count, truncated, columns, rows}``。低层 ``get_pool`` 仍保留给特殊需求。"""

    db_runtime_errors: tuple[type[BaseException], ...] = DB_RUNTIME_ERRORS
    """「DB / 运行时错误」分类元组，供插件收窄 except 范围、把它们包成带 rid 的
    RuntimeError，同时不吞掉编程错误（TypeError / KeyError 等）。"""

    plugin_name: str = ""
    """本插件的 entry-point 名（``ep.name``），由 ``load_plugins`` 在调 ``register`` 前注入。
    是本系统里插件的规范身份（与 ``plugins.disabled`` / 启动日志对齐）。插件用它做 ``source``
    标识、``getChild`` 子 logger 名等，避免硬编码自身名字。"""

    def db_errors(self, operation: str = "查询", *, logger: logging.Logger | None = None):
        """返回 async 上下文管理器，把 DB 异常包成带 rid 的 ``RuntimeError``。

        自动注入 rid（取 ``request_id_var``）与 ``db_runtime_errors``，免去插件重抄样板。用法::

            async with ctx.db_errors(logger=log):
                return await ctx.aexecute(sql, {...}, max_rows=..., source=...)

        ``operation`` 是面向客户端/日志的「什么失败了」标签，默认中性的「查询」即可——**不必放工具名**：
        客户端错误里的工具名由 FastMCP 的 ``Error executing tool <name>:`` 前缀自动提供（`<name>` 是该工具的
        注册名，对 ``@mcp.tool()`` / ``name=`` 覆盖 / sql_tools 的 ``add_tool(name=...)`` 都正确）。想要更具体的
        人类标签时可显式传 ``operation``。``logger`` 默认用 ``ctx.logger``，建议传插件自己的 ``getChild``
        子 logger 以保留命名空间。
        """
        return db_errors_as_client_error(
            logger=logger or self.logger,
            operation=operation,
            rid=self.request_id_var.get(),
            db_errors=self.db_runtime_errors,
        )
