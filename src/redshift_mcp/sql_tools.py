"""声明式 SQL 工具：把 ``config.sql_tools`` 里的声明动态注册成 MCP 工具（零代码）。

这是与 entry_points Python 插件并存的**第二种插件机制**：运维只在 config 里写
``{name, description, sql, params}``，启动时本模块据此为每条声明动态构造一个带正确
函数签名的工具函数（FastMCP 据签名 + 注解推断 inputSchema），再 ``mcp.add_tool`` 注册。

安全闸门：``spec.safe``（默认 True）在注册时把 SQL（占位符替换成中性字面量）过
``sql_guard.assert_read_only`` —— 要求单条只读查询，挡住运维误配的 DML/DDL/多语句。
"""
from __future__ import annotations

import inspect
import logging
import re
import typing
from datetime import datetime
from typing import Annotated, Any, Callable

from pydantic import Field

from . import db, sql_guard
from .config import SqlToolSpec
from .plugin import PluginContext

# 与插件同处 redshift_mcp.plugins 子树，复用主 handler / 测试 fixture 的 reset 列表。
logger = logging.getLogger("redshift_mcp.plugins.sql_tools")

# 参数类型 → Python 注解基类型。date 注解成 str（schema 为 string），格式由 _impl 另校验。
_PYTYPE: dict[str, type] = {"string": str, "int": int, "date": str}

# 匹配 psycopg 命名占位符 %(name)s；不会误匹配 %%（字面 percent）或 %s（位置占位符）。
_PLACEHOLDER = re.compile(r"%\([A-Za-z_]\w*\)s")

_POK = inspect.Parameter.POSITIONAL_OR_KEYWORD


def register_sql_tools(ctx: PluginContext) -> list[str]:
    """把 ``ctx.config.sql_tools`` 里的声明逐条注册成 MCP 工具，返回成功注册的工具名列表。

    - 与已注册工具（核心三件套 / 插件工具 / 先前的声明式工具）重名 → warn 跳过、不覆盖。
    - ``safe=True`` 且未通过只读闸门 → error 跳过该工具（不搞崩 server）。
    """
    registered: list[str] = []
    for spec in ctx.config.sql_tools:
        if spec.name in ctx.mcp._tool_manager._tools:
            logger.warning("声明式 SQL 工具名与已注册工具冲突，跳过: %s", spec.name)
            continue
        if spec.safe:
            try:
                # 占位符先替换成中性整数字面量，sqlglot 才能解析（执行仍用原始带占位符 SQL）。
                ast = sql_guard.assert_read_only(_PLACEHOLDER.sub("1", spec.sql))
            except ValueError as exc:
                logger.error(
                    "声明式 SQL 工具未通过安全闸门，已跳过: %s（%s）。"
                    "需为单条只读 SELECT；个别确需特殊语句的工具可设 safe: false。",
                    spec.name, exc,
                )
                continue
            # 闸门不自动加 LIMIT；顶层缺 LIMIT 时大表会被全量拉回内存再截断 →
            # 记 warn 把隐患显性化（不阻断注册，max_rows 仍会在拉回后截断）。
            if ast.args.get("limit") is None:
                logger.warning(
                    "声明式 SQL 工具 %s 的 SQL 顶层无 LIMIT；大结果集会被全量拉回内存"
                    "后再按 max_rows 截断，建议在 SQL 里自带 LIMIT。",
                    spec.name,
                )
        ctx.mcp.add_tool(
            _build_tool(spec, ctx), name=spec.name, description=spec.description
        )
        registered.append(spec.name)
        logger.info(
            "声明式 SQL 工具已注册: %s (%d 参数, safe=%s)",
            spec.name, len(spec.params), spec.safe,
        )

    logger.info(
        "声明式 SQL 工具加载完成，共 %d 个: %s",
        len(registered), ", ".join(registered) or "(无)",
    )
    return registered


def _build_tool(spec: SqlToolSpec, ctx: PluginContext) -> Callable[..., dict[str, Any]]:
    """据 spec 动态构造一个带正确 ``__signature__`` 的工具函数。"""
    log = logger.getChild(spec.name)

    # required 参数排前（Signature 要求有默认值的参数在后）；MCP 按 keyword 调用，顺序无碍。
    ordered = sorted(spec.params, key=lambda p: not p.required)
    sig_params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for p in ordered:
        base = typing.Literal[tuple(p.enum)] if p.type == "enum" else _PYTYPE[p.type]
        # 可选参数：注解包成 base | None，让 schema 表达「可空」，避免 int/enum 注解
        # 收到 None 默认值时被 pydantic 拒绝。描述包裹（Annotated）放在 Optional 外层。
        if not p.required:
            base = typing.Optional[base]
        desc = (p.description or "")
        if p.type == "date":
            desc = (desc + f"（格式 {p.format}）").strip()
        ann = Annotated[base, Field(description=desc)] if desc else base
        extra = {} if p.required else {"default": p.default}
        sig_params.append(inspect.Parameter(p.name, _POK, annotation=ann, **extra))
        annotations[p.name] = ann
    annotations["return"] = dict

    async def _impl(**kwargs: Any) -> dict[str, Any]:
        # int / enum 已由 FastMCP + pydantic 按 schema 上游校验；这里只补 date 格式校验。
        bind: dict[str, Any] = {}
        for p in spec.params:
            value = kwargs.get(p.name, p.default)
            if p.type == "date" and value is not None:
                try:
                    datetime.strptime(value, p.format)
                except ValueError as exc:
                    raise ValueError(
                        f"参数 {p.name} 日期格式不合法: {value!r}，期望 {p.format}。"
                    ) from exc
            bind[p.name] = value

        rid = ctx.request_id_var.get()
        try:
            # 阻塞 DB 调用走 db.aexecute（to_thread），不阻塞事件循环。
            return await db.aexecute(
                spec.sql, bind,
                max_rows=spec.max_rows or ctx.config.query.max_rows,
            )
        except ctx.db_runtime_errors as exc:
            log.exception("声明式 SQL 工具执行失败 tool=%s", spec.name)
            raise RuntimeError(
                f"查询失败 (request_id={rid}, 详见服务端日志): "
                f"{exc.__class__.__name__}"
            ) from exc

    _impl.__name__ = spec.name
    _impl.__doc__ = spec.description
    _impl.__signature__ = inspect.Signature(sig_params, return_annotation=dict)  # type: ignore[attr-defined]
    _impl.__annotations__ = annotations
    return _impl
