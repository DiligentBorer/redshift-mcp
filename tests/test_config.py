"""测试 redshift_mcp.config 模块。"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from redshift_mcp.config import (
    AppConfig,
    DatabaseConfig,
    LoggingConfig,
    QueryConfig,
    ServerConfig,
    SqlToolParam,
    SqlToolSpec,
    load_config,
    split_table_ref,
)


@pytest.mark.parametrize(
    "ref, expected",
    [
        ("schema.table", ("", "schema", "table")),
        ("db.schema.table", ("db", "schema", "table")),
        ("Analytics.Events", ("", "analytics", "events")),       # 归一小写
        ("DB.Schema.Table", ("db", "schema", "table")),
    ],
)
def test_split_table_ref_valid(ref, expected) -> None:
    assert split_table_ref(ref) == expected


@pytest.mark.parametrize(
    "bad",
    ["table", "a.b.c.d", "schema.", ".table", "a..c", "", "  "],
)
def test_split_table_ref_invalid(bad) -> None:
    with pytest.raises(ValueError):
        split_table_ref(bad)


# ===== 声明式 SQL 工具 schema 校验 =====


def test_sql_tool_param_defaults() -> None:
    p = SqlToolParam(name="date")
    assert p.type == "string" and p.required is True


def test_sql_tool_param_name_must_be_identifier() -> None:
    with pytest.raises(ValidationError):
        SqlToolParam(name="not ok")
    with pytest.raises(ValidationError):
        SqlToolParam(name="_leading")   # 不以 _ 开头（FastMCP 限制）


def test_sql_tool_param_enum_requires_values() -> None:
    with pytest.raises(ValidationError):
        SqlToolParam(name="kind", type="enum")          # 缺 enum
    SqlToolParam(name="kind", type="enum", enum=["a"])  # ok


def test_sql_tool_spec_defaults_safe_true() -> None:
    spec = SqlToolSpec(name="t", description="d", sql="SELECT 1")
    assert spec.safe is True and spec.params == [] and spec.max_rows is None


def test_query_timezone_defaults_utc_and_validates() -> None:
    assert QueryConfig().timezone == "UTC"
    QueryConfig(timezone="America/Los_Angeles")             # 合法 IANA 名通过
    with pytest.raises(ValidationError):
        QueryConfig(timezone="Mars/Phobos")                 # 非法时区被拒


def test_sql_tool_param_timezone_optional_and_validates() -> None:
    assert SqlToolParam(name="d", type="date").timezone is None   # 默认 None（用全局）
    SqlToolParam(name="d", type="date", timezone="Asia/Shanghai")  # 合法覆盖通过
    with pytest.raises(ValidationError):
        SqlToolParam(name="d", type="date", timezone="Nowhere/Land")


def test_sql_tool_param_rejects_unknown_field() -> None:
    # extra=forbid：写错字段名（如旧名 tz）在加载期直接报错，不静默忽略
    with pytest.raises(ValidationError):
        SqlToolParam(name="d", type="date", tz="Asia/Shanghai")


def test_sql_tool_spec_rejects_bad_name_and_empty_sql() -> None:
    with pytest.raises(ValidationError):
        SqlToolSpec(name="9bad", description="d", sql="SELECT 1")
    with pytest.raises(ValidationError):
        SqlToolSpec(name="t", description="d", sql="   ")

VALID_YAML = textwrap.dedent(
    """\
    database:
      host: db.example.com
      port: 5439
      dbname: warehouse
      user: ro_user
      password: secret
      sslmode: require
    server:
      host: 0.0.0.0
      port: 8000
      path: /redshift
      auth_token: "a-fine-token"
    query:
      statement_timeout_ms: 15000
      max_rows: 500
    logging:
      level: DEBUG
      file: /tmp/x.log
      max_bytes: 1048576
      backup_count: 2
      as_json: true
    """
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_full_config_ok(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, VALID_YAML))
    assert isinstance(cfg, AppConfig)
    assert cfg.database.host == "db.example.com"
    assert cfg.database.port == 5439
    assert cfg.server.path == "/redshift"
    assert cfg.server.auth_token == "a-fine-token"
    assert cfg.query.statement_timeout_ms == 15000
    assert cfg.query.max_rows == 500
    assert cfg.logging.level == "DEBUG"
    assert cfg.logging.as_json is True


def test_defaults_kick_in_when_optional_sections_missing(tmp_path: Path) -> None:
    minimal = textwrap.dedent(
        """\
        database:
          host: h
          dbname: d
          user: u
        server:
          auth_token: t
        """
    )
    cfg = load_config(_write(tmp_path, minimal))
    assert cfg.server.path == "/redshift"            # 默认值
    assert cfg.query.max_rows == 10000               # 默认值
    assert cfg.logging.level == "INFO"               # 默认值
    assert cfg.logging.file is None                  # 默认值


def test_empty_auth_token_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ServerConfig(auth_token="")
    assert "auth_token" in str(excinfo.value)


def test_whitespace_only_auth_token_rejected() -> None:
    with pytest.raises(ValidationError):
        ServerConfig(auth_token="   ")


def test_path_must_start_with_slash() -> None:
    with pytest.raises(ValidationError):
        ServerConfig(path="redshift", auth_token="t")


def test_logging_file_empty_string_becomes_none() -> None:
    cfg = LoggingConfig(file="")
    assert cfg.file is None
    cfg = LoggingConfig(file="   ")
    assert cfg.file is None
    cfg = LoggingConfig(file=None)
    assert cfg.file is None
    cfg = LoggingConfig(file="/var/log/x.log")
    assert cfg.file == "/var/log/x.log"


def test_missing_required_database_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        DatabaseConfig()  # 缺 host/dbname/user 必填字段


def test_missing_config_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_load_config_honours_env(monkeypatch, tmp_path: Path) -> None:
    p = _write(tmp_path, VALID_YAML)
    monkeypatch.setenv("REDSHIFT_MCP_CONFIG", str(p))
    cfg = load_config()  # 不传参 → 环境变量生效
    assert cfg.database.host == "db.example.com"


def test_invalid_sslmode_rejected() -> None:
    with pytest.raises(ValidationError):
        DatabaseConfig(host="h", dbname="d", user="u", sslmode="invalid-mode")
