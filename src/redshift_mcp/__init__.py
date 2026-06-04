"""应用版本的单一来源。

从已安装的包元数据（即 `pyproject.toml` 的 `version`）读取，
避免维护两份独立副本。仅当包未被安装时（比如直接从源码树跑、未
执行 `uv sync` / `pip install -e .`）才回退到哨兵值。
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("redshift-mcp")
except PackageNotFoundError:  # pragma: no cover - editable 安装缺失时进入
    __version__ = "0.0.0+unknown"
