"""插件框架：宿主→插件的契约（``PluginContext``）+ entry_points 加载器。

分发模型为「wheel + entry_points 安装式」：插件是独立的可安装包，在自己的
``pyproject.toml`` 里声明

    [project.entry-points."redshift_mcp.plugins"]
    <name> = "<import_pkg>:register"

装进与主程序同一个 venv 后，``load_plugins`` 通过
``importlib.metadata.entry_points(group="redshift_mcp.plugins")`` 自动发现，
逐个调用插件的 ``register(ctx)``。插件在 ``register`` 内用 ``ctx.mcp.tool()``
注册工具，并通过闭包捕获 ``ctx`` 拿到共享的连接池 / config / logger 等资源。

本模块**不依赖 ``server.py``**（只依赖 ``config`` / ``errors`` 两个叶子），
因此插件 import ``PluginContext`` 不会引入循环依赖。
"""
from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Callable, Iterable

from psycopg_pool import ConnectionPool

from .config import AppConfig
from .errors import DB_RUNTIME_ERRORS

if TYPE_CHECKING:  # 仅类型注解用；运行时不真正 import FastMCP（由 server.py 持有）
    from mcp.server.fastmcp import FastMCP

# 插件子树统一用这个 logger（及其 getChild）；它是 ``redshift_mcp`` 的子 logger，
# 默认 propagate=True，日志自然冒泡到主 handler，无需为插件单独配 handler。
logger = logging.getLogger("redshift_mcp.plugins")

# 插件 entry-point 的 group 名。插件 pyproject.toml 里要写成完全一致的字符串。
GROUP = "redshift_mcp.plugins"


@dataclass(frozen=True)
class PluginContext:
    """宿主注入给每个插件的共享上下文（**只读契约 / 公开 API，需保持稳定**）。

    破坏性修改本类字段会让已安装的插件失配，必须升主版本并在 CHANGELOG 标注。
    插件应只读使用本对象，不要修改其中的 ``config`` 等可变成员。
    """

    mcp: "FastMCP"
    """FastMCP 实例。插件用 ``ctx.mcp.tool()`` 装饰器（或 ``add_tool``）注册工具。"""

    config: AppConfig
    """应用配置。插件常用 ``ctx.config.query.max_rows`` 等。"""

    logger: logging.Logger
    """``redshift_mcp.plugins`` logger。插件建议用 ``ctx.logger.getChild("<name>")``。"""

    sql_audit_logger: logging.Logger
    """SQL 审计专用 logger（``redshift_mcp.sql_audit``），需要审计 SQL 文本的插件用。"""

    request_id_var: contextvars.ContextVar[str]
    """每请求关联 id 的 ContextVar（与 server.py 同一个对象）。在工具函数体内
    ``.get()`` 读到当前请求的 rid，用于面向客户端错误消息的可追溯字段。"""

    get_pool: Callable[[], ConnectionPool]
    """返回共享连接池的 callable（注入 ``db.get_pool``）；未初始化时抛 RuntimeError。"""

    db_runtime_errors: tuple[type[BaseException], ...] = DB_RUNTIME_ERRORS
    """「DB / 运行时错误」分类元组，供插件收窄 except 范围、把它们包成带 rid 的
    RuntimeError，同时不吞掉编程错误（TypeError / KeyError 等）。"""


def load_plugins(
    ctx: PluginContext,
    *,
    disabled: Iterable[str] = (),
) -> list[str]:
    """发现并加载所有注册到 ``redshift_mcp.plugins`` group 的插件。

    通过 ``importlib.metadata.entry_points`` 发现 —— 不扫描目录、不修改 ``sys.path``。
    每个插件的 import / 取 register / 调 register 三个动作各自 try/except 隔离：
    任一步失败都只记一条日志并跳过该插件，**绝不让坏插件搞崩整个 server**。

    Args:
        ctx: 注入给插件的共享上下文。
        disabled: 「已安装但不启用」的插件名集合（来自 ``config.plugins.disabled``）。

    Returns:
        成功加载（register 正常返回）的插件名列表。
    """
    disabled_set = set(disabled)
    loaded: list[str] = []

    for ep in entry_points(group=GROUP):
        if ep.name in disabled_set:
            logger.info("插件已在 config 中禁用，跳过: %s", ep.name)
            continue

        try:
            obj = ep.load()
        except Exception:
            logger.exception("插件 import 失败，已跳过: %s (%s)", ep.name, ep.value)
            continue

        # entry-point 目标既支持 "pkg:register"（直接指向函数），
        # 也支持 "pkg"（取模块的 .register 属性）。
        register = obj if callable(obj) else getattr(obj, "register", None)
        if not callable(register):
            logger.error("插件无可调用的 register，已跳过: %s (%s)", ep.name, ep.value)
            continue

        try:
            register(ctx)
        except Exception:
            logger.exception("插件 register() 失败，已跳过: %s", ep.name)
            continue

        loaded.append(ep.name)
        logger.info("插件已加载: %s (%s)", ep.name, ep.value)

    logger.info("插件加载完成，共 %d 个: %s", len(loaded), ", ".join(loaded) or "(无)")
    return loaded
