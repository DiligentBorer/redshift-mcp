"""SQL 安全闸门：用 sqlglot 解析 AST，仅放行符合规则的查询。

规则:
1. 必须能被 sqlglot 以 Redshift 方言解析
2. 必须**单条**语句
3. 顶层必须是查询（``exp.Query`` 子类，含 ``Select`` / ``Union`` / ``Intersect`` / ``Except``）；
   拒绝 INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / SET 等命令
4. 显式拒绝任何 ``SELECT INTO`` / ``SELECT INTO TEMP``（防御未来权限漂移）
5. 查询内所有引用的表必须**全限定** schema.table，并且都在白名单内
6. CTE 别名不参与白名单校验（属于 in-query 局部命名空间）

通过校验后返回 AST，调用方可以基于 AST 继续做安全的改写（如追加 LIMIT）。
"""
from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

logger = logging.getLogger(__name__)

# 这些顶层节点类型属于明确"非 SELECT"，单独列出便于给出更精准的错误消息
_FORBIDDEN_TOPLEVEL: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Set,
    exp.Use,
    exp.Show,
    exp.Command,   # 兜底其它"命令式"语句
)


def assert_read_only(sql: str) -> exp.Expression:
    """校验 ``sql`` 是**单条只读查询**（不含白名单约束），通过返回顶层 AST。

    解析 + 单条 + 拒 DML/DDL/SET/SHOW 等命令式 + 必须 ``exp.Query``（含 Select /
    Union / Intersect / Except）+ 拒任何 ``SELECT INTO``。违规抛 ``ValueError``。

    供两处复用：``validate_select_only``（run_sql，叠加白名单）与声明式 SQL 工具的
    安全闸门（``sql_tools``，仅需「只读」校验、不约束表）。
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL 不能为空")

    try:
        parsed = sqlglot.parse(sql, read="redshift")
    except ParseError as exc:
        raise ValueError(f"SQL 解析失败: {exc}") from exc

    # sqlglot 会在多语句之间产生 None 占位；过滤掉
    statements = [s for s in parsed if s is not None]
    if not statements:
        raise ValueError("SQL 不能为空")
    if len(statements) > 1:
        raise ValueError(f"只允许单条 SQL 语句，收到 {len(statements)} 条")

    ast = statements[0]

    # 拒绝顶层命令式语句（DML / DDL / SET / SHOW 等）
    if isinstance(ast, _FORBIDDEN_TOPLEVEL):
        raise ValueError(
            f"只允许查询语句，收到 {type(ast).__name__.upper()}"
        )
    # 放宽到 exp.Query —— 含 Select / Union / Intersect / Except
    if not isinstance(ast, exp.Query):
        raise ValueError(
            f"只允许 SELECT / UNION / INTERSECT / EXCEPT，收到 {type(ast).__name__}"
        )

    # 拒绝任何 SELECT INTO（含 INTO TEMP / TEMPORARY / #tmp 形式）
    # defense-in-depth：当前 Redshift 只读账号无 CREATE 权限会兜底，但
    # 显式拦截避免未来权限配错时立刻失守。子查询里的 INTO 也一并拦截。
    if any(ast.find_all(exp.Into)):
        raise ValueError(
            "不允许 SELECT INTO（含 INTO TEMP）；只支持纯查询的 SELECT"
        )

    return ast


def validate_select_only(
    sql: str,
    allowed_tables: set[str],
    default_database: str | None = None,
) -> exp.Expression:
    """对外唯一入口：校验通过返回 AST，违规抛 ``ValueError``。

    在 ``assert_read_only``（单条只读查询）基础上，再强制所有引用表全限定且在白名单内。

    白名单与 SQL 引用统一在「数据库前缀 + schema.table」层面比对：

    - 传入 ``default_database`` 时（``run_sql`` 的真实路径），每处引用的归一键是
      ``(catalog or default_database).schema.table`` —— 没写库前缀的默认用配置库补全，
      显式写了别的库（``otherdb.schema.table``）则保留该库前缀，从而与三段式白名单
      做严格匹配、挡住跨库越权读取。
    - 不传 ``default_database`` 时（旧调用 / 部分单测），保持原两段式 ``schema.table``
      归一（不引入库前缀），向后兼容。

    Args:
        sql: 待校验的 SQL 字符串。
        allowed_tables: 归一化后的白名单全限定表名集合（小写；三段或两段，与
            ``default_database`` 是否传入对应）。
        default_database: 配置的默认 database 名（``cfg.database.dbname``）；用于给
            未写库前缀的引用补全，并统一成三段式比对。

    Returns:
        sqlglot Expression 顶层节点（必为 ``Select``）。
    """
    ast = assert_read_only(sql)

    # 收集 CTE 别名（in-query 局部命名空间，不参与白名单校验）
    cte_aliases: set[str] = set()
    for cte in ast.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            cte_aliases.add(alias.lower())

    default_db = (default_database or "").lower()

    # 遍历所有 Table 引用（含 FROM / JOIN / 子查询）
    referenced: set[str] = set()
    bare_refs: set[str] = set()
    for table in ast.find_all(exp.Table):
        schema = (table.db or "").lower()
        tname = (table.name or "").lower()
        if not tname:
            continue
        # CTE 引用：schema 为空且名称匹配 CTE 别名 → 跳过
        if not schema and tname in cte_aliases:
            continue
        if not schema:
            bare_refs.add(tname)
            continue
        # 库前缀 = 显式 catalog，否则用配置默认库补全（仅当传了 default_database）。
        prefix = (table.catalog or "").lower() or default_db
        referenced.add(f"{prefix}.{schema}.{tname}" if prefix else f"{schema}.{tname}")

    if bare_refs:
        raise ValueError(
            f"SQL 含未限定 schema 的表引用 {sorted(bare_refs)}; "
            f"所有表必须以 schema.table 形式书写完整。"
            f"请先调用 list_tables 查看可用表全名。"
        )

    unauthorized = referenced - allowed_tables
    if unauthorized:
        whitelist_repr = sorted(allowed_tables) if allowed_tables else "(空)"
        raise ValueError(
            f"SQL 访问了不在白名单内的表 {sorted(unauthorized)}; "
            f"白名单为 {whitelist_repr}。"
            f"请先调用 list_tables 查看可用表全名。"
        )

    return ast


def apply_row_cap(ast: exp.Expression, max_rows: int) -> str:
    """对已经过校验的查询 AST 追加 / 收紧 LIMIT，返回最终 SQL 字符串。

    策略：cap = max_rows + 1（多取 1 行用于判断是否截断）。
    - 若 AST 顶层无 LIMIT，则直接追加 cap
    - 若已带 LIMIT 且数值可解析为整数，取 ``min(已有, cap)``
    - 已带 LIMIT 但无法解析为整数（动态参数等），保守地覆盖为 cap

    适用于 ``exp.Query`` 的全部子类（Select / Union / Intersect / Except）；
    实测 sqlglot 的 ``.limit()`` 在 Union 上也正确工作。
    """
    if not isinstance(ast, exp.Query):
        # 防御性：经 validate_select_only 后理论上不会到这里
        return ast.sql(dialect="redshift")

    cap = max_rows + 1
    existing = ast.args.get("limit")
    if existing is not None:
        inner = existing.expression
        try:
            existing_val = int(str(inner.name)) if inner is not None else None
        except (ValueError, AttributeError, TypeError):
            existing_val = None
        if existing_val is not None:
            cap = min(existing_val, cap)
        # else: 保留 cap 不变，覆盖原 LIMIT

    capped = ast.limit(cap)
    return capped.sql(dialect="redshift")
