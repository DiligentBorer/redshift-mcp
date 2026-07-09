"""redshift-mcp 插件契约层 SDK。

外部插件只依赖本包（不引入 host 实现源码）即可拿到宿主→插件契约::

    from redshift_mcp_sdk import PluginContext

host（``redshift-mcp``）与所有插件共享同一份契约；host 自己也从本包 import
``PluginContext`` 来构造并注入。详见 :mod:`redshift_mcp_sdk.plugin`。
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .plugin import GROUP, PluginContext

try:
    __version__ = version("redshift-mcp-sdk")
except PackageNotFoundError:  # 源码树里未安装分发包时（罕见）降级，不影响契约可用
    __version__ = "0.0.0"

__all__ = ["GROUP", "PluginContext", "__version__"]
