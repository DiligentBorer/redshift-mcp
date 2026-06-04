"""共享异常定义。

这是一个**零依赖叶子模块**：只 import 标准库 + psycopg，不 import 本项目任何
其它模块。把 ``DB_RUNTIME_ERRORS`` 放在这里（而不是 ``server.py``），是为了让
外部插件能复用同一组「DB / 运行时错误」分类，而**不必 import ``server.py``** ——
否则会形成 server → plugin → 插件 → server 的循环依赖。
"""
from __future__ import annotations

import psycopg

# 工具 / 插件在调用 DB 的路径上，应把这一组「DB / 运行时错误」包装成带 rid 的
# RuntimeError 抛给客户端，但**不**吞掉编程错误（TypeError / KeyError / sqlglot
# 内部断言等）—— 让那些 bug 类异常原样冒泡，由 FastMCP 包成 500，便于早暴露。
DB_RUNTIME_ERRORS: tuple[type[BaseException], ...] = (
    psycopg.Error,
    RuntimeError,
    ConnectionError,
    TimeoutError,
)
