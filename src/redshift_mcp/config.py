from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    name: str   # 全限定 schema.table，会被归一为小写
    description: str | None = None
    columns: dict[str, ColumnSpec] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_has_schema(cls, v: str) -> str:
        if not isinstance(v, str) or v.count(".") != 1 or v.startswith(".") or v.endswith("."):
            raise ValueError("表名必须是 schema.table 格式（恰好一个点，且两侧非空）")
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

    @cached_property
    def allowed_table_names_set(self) -> frozenset[str]:
        """白名单的归一化全限定名集合（schema.table，小写），缓存一次。"""
        return frozenset(t.name for t in self.tables)

    def allowed_table_names(self) -> set[str]:
        """返回白名单中所有表的归一化全限定名集合（小写 schema.table）。

        保留函数形式向后兼容；底层走 ``allowed_table_names_set`` 缓存。
        """
        return set(self.allowed_table_names_set)


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        path = os.environ.get("REDSHIFT_MCP_CONFIG", "config.yaml")
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)
