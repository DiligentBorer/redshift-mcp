"""插件加载器：发现并加载注册到 ``redshift_mcp.plugins`` group 的插件。

契约（``PluginContext`` / ``GROUP``）已抽到独立 SDK 包 ``redshift_mcp_sdk``（让外部插件只依赖
薄 SDK、不引入 host 实现源码）。本模块从 SDK **re-export** 它们做 back-compat（host 内既有
``from redshift_mcp.plugin import PluginContext`` 与测试不变），并保留 host 运行时才用的加载器
``load_plugins`` / ``iter_installed_plugins`` —— 这两个用 ``importlib.metadata`` 发现插件，是
host 运行时职责，**插件永不需要**，故不放进 SDK。
"""
from __future__ import annotations

import logging
from dataclasses import replace
from importlib.metadata import entry_points
from typing import Iterable

# 契约层从 SDK re-export（back-compat：现有 import 路径不变、身份断言不变）。
from redshift_mcp_sdk import GROUP, PluginContext

__all__ = ["GROUP", "PluginContext", "load_plugins", "iter_installed_plugins"]

# 插件子树统一用这个 logger（及其 getChild）；它是 ``redshift_mcp`` 的子 logger，
# 默认 propagate=True，日志自然冒泡到主 handler，无需为插件单独配 handler。
logger = logging.getLogger("redshift_mcp.plugins")


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

        # 每插件专属 ctx：注入其 entry-point 名，让插件用 ctx.plugin_name 而非硬编码自名。
        try:
            register(replace(ctx, plugin_name=ep.name))
        except Exception:
            logger.exception("插件 register() 失败，已跳过: %s", ep.name)
            continue

        loaded.append(ep.name)
        logger.info("插件已加载: %s (%s)", ep.name, ep.value)

    logger.info("插件加载完成，共 %d 个: %s", len(loaded), ", ".join(loaded) or "(无)")
    return loaded


def iter_installed_plugins() -> list[tuple[str, str, str]]:
    """列出所有注册到 ``redshift_mcp.plugins`` group 的已装插件 ``(ep.name, dist 名, version)``。

    供 server 的 ``--list-plugins`` 免启动展示（运维据此得知 ``plugins.disabled`` 该填什么名）。
    纯函数、无副作用：只读 entry-point 元数据，**不 import 插件、不连 DB**。
    """
    out: list[tuple[str, str, str]] = []
    for ep in entry_points(group=GROUP):
        dist = ep.dist
        out.append((
            ep.name,
            dist.name if dist is not None else "?",
            dist.version if dist is not None else "?",
        ))
    return out
