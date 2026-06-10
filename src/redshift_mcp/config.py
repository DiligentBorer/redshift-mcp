from __future__ import annotations

import copy
import logging
import os
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


def split_table_ref(table: str) -> tuple[str, str, str]:
    """解析 ``schema.table`` / ``database.schema.table`` → ``(catalog, schema, table)`` 小写三元组。

    ``catalog`` 为空串表示未写库前缀（两段式）。格式非法（段数不为 2/3、或存在空白段）抛
    ``ValueError``（消息**不含** ``list_tables`` 提示，由调用方按需追加）。

    供 ``describe_table``（server.py）与 ``TableSpec`` 名称校验共用，保证两边「段数 + 非空 + 小写」
    规则一致。
    """
    parts = table.split(".") if isinstance(table, str) else []
    if len(parts) not in (2, 3) or any(not p.strip() for p in parts):
        raise ValueError(
            f"表名必须是 schema.table 或 database.schema.table 格式（各段非空）: {table!r}"
        )
    catalog = parts[0].lower() if len(parts) == 3 else ""
    return catalog, parts[-2].lower(), parts[-1].lower()


class DatabaseConfig(BaseModel):
    host: str
    port: int = 5439
    dbname: str
    user: str
    password: str = ""
    sslmode: Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] = "require"
    pool_min_size: int = Field(default=1, ge=0)
    pool_max_size: int = Field(default=5, ge=1)
    connect_timeout: int = Field(default=10, ge=1)

    @field_validator("pool_max_size")
    @classmethod
    def _max_ge_min(cls, v: int, info) -> int:
        min_size = info.data.get("pool_min_size", 0)
        if v < min_size:
            raise ValueError("pool_max_size 必须 >= pool_min_size")
        return v


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    path: str = "/redshift"
    auth_token: str

    @field_validator("path")
    @classmethod
    def _path_starts_with_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("server.path 必须以 '/' 开头")
        return v

    @field_validator("auth_token")
    @classmethod
    def _auth_token_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("server.auth_token 必须是非空字符串")
        return v


class QueryConfig(BaseModel):
    # 默认 60s —— 宽事实表上 Error API 查询的典型未缓存耗时实测约 15s；
    # 60s 留有 ~4x 安全裕度，且能覆盖冷启动峰值。若集群 / WLM 倾向于把
    # 这种扫描密集查询排队，可调高；若想更快得到失败反馈，可调低。
    statement_timeout_ms: int = Field(default=60000, ge=1)
    max_rows: int = Field(default=10000, ge=1)


class ColumnSpec(BaseModel):
    """单列的可选补充说明（叠加到从 DB 拉到的 schema 之上）。"""

    description: str | None = None
    example_values: list[str] | None = None


class TableSpec(BaseModel):
    """白名单中一张允许查询的表。"""

    name: str   # 全限定 schema.table 或 database.schema.table，会被归一为小写
    description: str | None = None
    columns: dict[str, ColumnSpec] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_has_schema(cls, v: str) -> str:
        # 支持两段（schema.table）或三段（database.schema.table）。两段式在
        # AppConfig.allowed_table_names_set 归一时用 database.dbname 补全前缀。
        # 复用 split_table_ref 的「段数 + 非空」校验，与 describe_table 保持一致规则。
        split_table_ref(v)
        return v.lower()


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # 滚动日志文件路径。None 或空字符串 => 只输出到 stderr。
    file: str | None = None
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)  # 10 MB
    backup_count: int = Field(default=5, ge=0)
    as_json: bool = False  # true => 输出 JSON 行格式（而非纯文本）

    # ---- SQL 审计专用通道（run_sql 的完整 SQL 文本走这里）----
    # 与 level 正交：默认 WARNING 意味着 INFO/DEBUG 级 SQL 不输出（PII 安全）；
    # 改成 INFO 即可观察到每条 run_sql 的完整 SQL。修改本字段不会影响其他
    # logger（uvicorn / mcp 等）的输出量。
    sql_audit_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    # 审计文件路径（独立于 file）。
    # None / 空 => 与运行日志合流：audit 走和 main 同一组 handler（stderr + 可选 file），
    #              handler.level 自动取 min(level, sql_audit_level) 让 audit 能穿过。
    # 非空     => audit 独占自己的 stderr_audit + file_audit handler，与 main 完全分离，
    #              便于做单独 retention / 加密 / SIEM 接入。
    sql_audit_file: str | None = None

    @field_validator("file", "sql_audit_file", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v


class PluginsConfig(BaseModel):
    """插件加载配置。

    分发模型为 entry_points 安装式：venv 里装了哪个注册到
    ``redshift_mcp.plugins`` group 的插件，启动时就会被自动发现并启用 ——
    没有「插件目录」概念。``disabled`` 给运维一个「已安装但临时不启用」
    的关闭开关；``enabled=false`` 则整体跳过插件加载。
    """

    enabled: bool = True
    disabled: list[str] = Field(default_factory=list)


class SqlToolParam(BaseModel):
    """声明式 SQL 工具的单个参数（type/format/enum 用于注册时构造 schema + 调用时校验）。"""

    name: str
    type: Literal["string", "int", "date", "enum"] = "string"
    description: str | None = None
    required: bool = True
    format: str = "%Y-%m-%d"            # 仅 type=date 用，strptime 格式
    enum: list[str] | None = None       # type=enum 时必填非空
    default: str | int | None = None    # required=false 时的默认值

    @field_validator("name")
    @classmethod
    def _name_is_identifier(cls, v: str) -> str:
        # 参数名要当成 Python 函数参数名（FastMCP 据签名推断 schema），不能以 _ 开头。
        if not isinstance(v, str) or not v.isidentifier() or v.startswith("_"):
            raise ValueError(f"参数名必须是合法标识符且不以 '_' 开头: {v!r}")
        return v

    @model_validator(mode="after")
    def _enum_required_when_enum_type(self) -> "SqlToolParam":
        if self.type == "enum" and not self.enum:
            raise ValueError(f"参数 {self.name!r} 的 type=enum 时必须提供非空 enum 列表")
        return self


class SqlToolSpec(BaseModel):
    """声明式 SQL 工具：在 config 里声明，启动时由 sql_tools.register_sql_tools 注册成 MCP 工具。"""

    name: str                                       # 工具名（= MCP tool name），合法标识符
    description: str                                # 给 LLM 看的说明
    sql: str                                        # 用 %(param)s 命名占位符；sql_file 在 load_config 已内联
    params: list[SqlToolParam] = Field(default_factory=list)
    max_rows: int | None = Field(default=None, ge=1)  # 覆盖全局 query.max_rows
    safe: bool = True                               # 安全闸门：默认开（注册时校验单条只读 SELECT）

    @field_validator("name")
    @classmethod
    def _name_is_identifier(cls, v: str) -> str:
        if not isinstance(v, str) or not v.isidentifier() or v.startswith("_"):
            raise ValueError(f"工具名必须是合法标识符且不以 '_' 开头: {v!r}")
        return v

    @field_validator("description", "sql")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("不能为空")
        return v


class AppConfig(BaseModel):
    # 让 pydantic 把 cached_property 当作普通方法属性而非 model field
    model_config = ConfigDict(ignored_types=(cached_property,))

    database: DatabaseConfig
    server: ServerConfig
    query: QueryConfig = QueryConfig()
    logging: LoggingConfig = LoggingConfig()
    plugins: PluginsConfig = PluginsConfig()
    # 通用查询能力的表白名单；为空时 list_tables / describe_table / run_sql
    # 三个工具仍然注册，但都会拒绝（白名单为空）。
    tables: list[TableSpec] = Field(default_factory=list)
    # 声明式 SQL 工具：零代码，直接在 config 里声明 → 启动注册成 MCP 工具。
    sql_tools: list[SqlToolSpec] = Field(default_factory=list)

    @cached_property
    def tables_by_norm(self) -> dict[str, TableSpec]:
        """三段式归一键（``database.schema.table``，小写）→ TableSpec 映射，缓存一次。

        两段式白名单条目用 ``database.dbname`` 补全 database 前缀；三段式原样。
        ``describe_table`` 用它做白名单成员判断 + 取列说明 spec；``run_sql``
        的白名单集合也由此派生。统一三段式后，SQL 引用无论写两段还是三段都能
        按同一规则归一比对，挡住跨库（``otherdb.schema.table``）越权读取。
        """
        db = self.database.dbname.lower()
        out: dict[str, TableSpec] = {}
        for t in self.tables:
            parts = t.name.split(".")
            key = f"{db}.{t.name}" if len(parts) == 2 else t.name
            out[key] = t
        return out

    @cached_property
    def allowed_table_names_set(self) -> frozenset[str]:
        """白名单的三段式归一全限定名集合（``database.schema.table``，小写），缓存一次。"""
        return frozenset(self.tables_by_norm)

    def allowed_table_names(self) -> set[str]:
        """返回白名单中所有表的三段式归一全限定名集合（小写 ``database.schema.table``）。

        保留函数形式向后兼容；底层走 ``allowed_table_names_set`` 缓存。
        """
        return set(self.allowed_table_names_set)

    def normalize_table_ref(self, catalog: str, schema: str, table: str) -> str:
        """把一处表引用归一成三段式键（小写）。``catalog`` 为空时用 ``database.dbname`` 补全。

        ``run_sql`` 闸门与 ``describe_table`` 共用这条归一规则，保证两边对称。
        """
        db = (catalog or self.database.dbname).lower()
        return f"{db}.{schema.lower()}.{table.lower()}"


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是映射(dict): {path}")
    return data


def _inline_sql_file(obj: dict, base_dir: Path, where: str) -> None:
    """把 dict 里的 sql_file 读成 sql（相对 base_dir）；sql 与 sql_file 不可并存。"""
    if not isinstance(obj, dict):
        return
    if obj.get("sql_file") and obj.get("sql"):
        raise ValueError(f"{where} 不能同时配置 sql 和 sql_file（二选一）")
    sql_file = obj.pop("sql_file", None)
    if sql_file:
        p = base_dir / sql_file
        if not p.exists():
            raise FileNotFoundError(f"{where} 的 sql_file 不存在: {p}")
        obj["sql"] = p.read_text(encoding="utf-8")


def _resolve_sql_files(raw: dict, base_dir: Path) -> dict:
    """把 raw 中 host sql_tools 条目的 sql_file 内联为 sql（相对 base_dir）。

    仅处理宿主自有的 sql_tools；插件私有 SQL 内聚在插件内部，不在此解析。
    """
    raw = copy.deepcopy(raw)
    for i, entry in enumerate(raw.get("sql_tools") or []):
        _inline_sql_file(entry, base_dir, f"sql_tools[{i}]")
    return raw


def _deep_merge(base: dict, overlay: dict) -> dict:
    """递归合并两个 dict：嵌套 dict 深合并、list 追加、标量 overlay 覆盖。"""
    out = dict(base)
    for k, v in overlay.items():
        cur = out.get(k)
        if isinstance(cur, dict) and isinstance(v, dict):
            out[k] = _deep_merge(cur, v)
        elif isinstance(cur, list) and isinstance(v, list):
            out[k] = cur + v
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载配置。支持顶层 ``include`` 把片段文件合并进来、以及 sql_tools 的 ``sql_file`` 外链。

    合并规则：``include`` 仅主配置生效（不支持嵌套）；glob 相对主配置目录、结果排序保证确定性；
    片段按「list 追加 / 嵌套 dict 深合并 / 标量片段覆盖」并入主配置。``sql_file`` 相对**声明它的
    那个文件**所在目录解析。最终仍产出单个 ``AppConfig``，对下游透明。
    """
    if path is None:
        path = os.environ.get("REDSHIFT_MCP_CONFIG", "config.yaml")
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    main = _read_yaml(path)
    includes = main.pop("include", None) or []      # include 仅主配置生效
    if isinstance(includes, str):
        includes = [includes]
    merged = _resolve_sql_files(main, path.parent)   # 主配置 sql_file 相对主配置目录

    seen: set[Path] = set()
    for pattern in includes:
        matched = sorted(path.parent.glob(pattern), key=str)
        if not matched:
            logger.warning("include 模式未匹配到任何文件: %s", pattern)
        for fp in matched:
            if fp in seen:
                continue
            seen.add(fp)
            frag = _read_yaml(fp)
            frag.pop("include", None)                # 不支持嵌套 include
            merged = _deep_merge(merged, _resolve_sql_files(frag, fp.parent))

    return AppConfig.model_validate(merged)
