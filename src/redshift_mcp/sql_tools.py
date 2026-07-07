"""声明式 SQL 工具：把 ``config.sql_tools`` 里的声明动态注册成 MCP 工具（零代码）。

这是与 entry_points Python 插件并存的**第二种插件机制**：运维只在 config 里写
``{name, description, sql, params}``，启动时本模块据此为每条声明动态构造一个带正确
函数签名的工具函数（FastMCP 据签名 + 注解推断 inputSchema），再 ``mcp.add_tool`` 注册。

安全闸门：``spec.safe``（默认 True）在注册时把 SQL（占位符替换成中性字面量）过
``sql_guard.assert_read_only`` —— 要求单条只读查询，挡住运维误配的 DML/DDL/多语句。

LIMIT 自动下推（仅 ``safe=True``）：复用闸门产出的 AST 判断顶层是否有 LIMIT —— 缺则
注册期用 ``_append_limit`` 文本追加 ``LIMIT (max_rows+1)`` 下推到 DB（避免大结果集全量
拉回内存）；显式写了 LIMIT 则原样尊重、不收紧。
"""
from __future__ import annotations

import inspect
import logging
import re
import typing
from datetime import datetime
from typing import Annotated, Any, Callable
from zoneinfo import ZoneInfo

from pydantic import Field

from . import db, sql_guard
from .config import SqlToolSpec
from .plugin import PluginContext

# 与插件同处 redshift_mcp.plugins 子树，复用主 handler / 测试 fixture 的 reset 列表。
logger = logging.getLogger("redshift_mcp.plugins.sql_tools")

# 参数类型 → Python 注解基类型。date 注解成 str（schema 为 string），格式由 _impl 另校验。
_PYTYPE: dict[str, type] = {"string": str, "int": int, "date": str}

# 匹配 psycopg 命名占位符 %(name)s；捕获组取 name。不会误匹配 %%（字面 percent）或 %s（位置占位符）。
_PLACEHOLDER = re.compile(r"%\(([A-Za-z_]\w*)\)s")

_POK = inspect.Parameter.POSITIONAL_OR_KEYWORD


def _append_limit(sql: str, cap: int) -> str:
    """在 SQL 末尾另起一行追加 ``LIMIT cap``，返回新 SQL（不碰原文其余部分）。

    - ``cap`` 是 server 侧受控整数（config ``max_rows`` 经 ``ge=1`` 校验），直接内联
      字面量、无注入面，无需走 bind 参数（避免与用户参数名冲突）。
    - **另起一行**是关键：原 SQL 若以 ``-- 行注释`` 结尾，LIMIT 落在新行不会被吞掉。
    - 去掉尾部空白与可能的结尾 ``;``（``assert_read_only`` 已保证单条语句，安全）。

    仅在调用方确认顶层无 LIMIT 时调用 —— 此分支追加的 LIMIT 与 ``apply_row_cap`` 的
    「顶层无 LIMIT 则追加 cap」一致（UNION / ORDER BY 顶层追加同样绑定到整个查询）。
    **但两者对「已有 LIMIT」的策略不同**：``apply_row_cap``（run_sql）会收紧到
    ``min(已有, cap)``；声明式工具显式写了 LIMIT 时本函数不被调用、原样尊重不收紧。
    """
    stripped = sql.rstrip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    return f"{stripped}\nLIMIT {cap}"


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
        # 占位符校验：psycopg 执行时会扫描整个 SQL（含注释）找 %(name)s，凡出现但未在
        # params 声明的占位符（最常见是误写进注释、或漏声明），运行期必抛 KeyError。这里
        # 在注册时就 fail-fast：记 error + 跳过该工具（与 safe 闸门、重名冲突同样不搞崩 server）。
        declared = {p.name for p in spec.params}
        used = set(_PLACEHOLDER.findall(spec.sql))
        undeclared = used - declared
        if undeclared:
            logger.error(
                "声明式 SQL 工具 %s 的 SQL 引用了未在 params 声明的占位符 %s，已跳过。"
                "请检查：是否误写进注释（psycopg 连注释也扫描占位符）/ 是否漏声明该 params / "
                "字面 %% 需写成 %%%%。",
                spec.name, sorted(undeclared),
            )
            continue
        effective_max = spec.max_rows or ctx.config.query.max_rows
        # 默认执行原始带占位符 SQL；safe=True 且顶层缺 LIMIT 时下面会替换成追加版。
        capped_sql = spec.sql
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
            # 顶层缺 LIMIT → 自动追加 LIMIT (max_rows + 1) 下推到 DB，避免大结果集
            # 全量拉回内存再截断；显式写了 LIMIT 则原样尊重（不收紧）。
            if ast.args.get("limit") is None:
                cap = effective_max + 1
                capped_sql = _append_limit(spec.sql, cap)
                logger.info(
                    "声明式 SQL 工具 %s 顶层无 LIMIT，已自动追加 LIMIT %d 下推到 DB。",
                    spec.name, cap,
                )
        ctx.mcp.add_tool(
            _build_tool(spec, ctx, capped_sql, effective_max),
            name=spec.name, description=spec.description,
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


def _build_tool(
    spec: SqlToolSpec,
    ctx: PluginContext,
    capped_sql: str,
    effective_max: int,
) -> Callable[..., dict[str, Any]]:
    """据 spec 动态构造一个带正确 ``__signature__`` 的工具函数。

    ``capped_sql`` 是注册期预生成的执行 SQL（顶层缺 LIMIT 时已追加 ``LIMIT
    effective_max+1``，否则即 ``spec.sql``）；``effective_max`` 是该工具的有效行上限
    （``spec.max_rows or config.query.max_rows``），用于客户端侧截断判断。
    """
    log = logger.getChild(spec.name)

    # 可选（required=false）且未写显式 default 的 date 参数：省略调用时按「有效时区的今天」
    # 兜底。有效时区 = 参数级 timezone 覆盖 or 全局 query.timezone；已在 config 校验过，
    # 这里构造 ZoneInfo 不会失败。静态解析一次、闭包捕获，避免每次调用重建。
    global_tz = ctx.config.query.timezone
    dynamic_date_tz: dict[str, ZoneInfo] = {
        p.name: ZoneInfo(p.timezone or global_tz)
        for p in spec.params
        if p.type == "date" and not p.required and p.default is None
    }

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
            if p.name in dynamic_date_tz:
                desc += f"（省略则默认为 {p.timezone or global_tz} 时区的今天）"
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
            if p.type == "date":
                if value is None and p.name in dynamic_date_tz:
                    # 可选 date 参数被省略且无显式 default → 取有效时区的今天。
                    # strftime(p.format) 生成，天然符合 format，无需再校验。
                    value = datetime.now(dynamic_date_tz[p.name]).strftime(p.format)
                elif value is not None:
                    try:
                        datetime.strptime(value, p.format)
                    except ValueError as exc:
                        raise ValueError(
                            f"参数 {p.name} 日期格式不合法: {value!r}，期望 {p.format}。"
                        ) from exc
            bind[p.name] = value

        # 阻塞 DB 调用走 db.aexecute（to_thread），不阻塞事件循环。
        # 执行用注册期预生成的 capped_sql（可能已追加 LIMIT），行截断用 effective_max。
        # DB 异常包装复用 ctx.db_errors（自动注入 rid + db_runtime_errors）；operation 用默认即可
        # ——客户端错误里的工具名由 FastMCP 前缀提供。source 带 sql_tools: 前缀，进完成日志/审计便于按来源筛。
        source = f"sql_tools:{spec.name}"
        async with ctx.db_errors(logger=log):
            return await db.aexecute(
                capped_sql, bind, max_rows=effective_max, source=source
            )

    _impl.__name__ = spec.name
    _impl.__doc__ = spec.description
    _impl.__signature__ = inspect.Signature(sig_params, return_annotation=dict)  # type: ignore[attr-defined]
    _impl.__annotations__ = annotations
    return _impl
