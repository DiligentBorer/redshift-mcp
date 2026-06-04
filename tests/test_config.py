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
    ServerConfig,
    load_config,
)

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
