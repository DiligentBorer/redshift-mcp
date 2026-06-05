"""测试 config.py 里 TableSpec / ColumnSpec / AppConfig.tables 的校验逻辑。"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from redshift_mcp.config import (
    AppConfig,
    ColumnSpec,
    TableSpec,
    load_config,
)


def test_tablespec_name_normalized_to_lower() -> None:
    t = TableSpec(name="Analytics.Events")
    assert t.name == "analytics.events"


def test_tablespec_name_without_schema_rejected() -> None:
    with pytest.raises(ValidationError, match="schema.table"):
        TableSpec(name="t_action_info")


def test_tablespec_name_three_part_accepted() -> None:
    """三段式 database.schema.table 是合法白名单条目，归一为小写。"""
    t = TableSpec(name="MyDB.Analytics.t_x")
    assert t.name == "mydb.analytics.t_x"


def test_tablespec_name_four_part_rejected() -> None:
    with pytest.raises(ValidationError, match="schema.table"):
        TableSpec(name="a.b.c.d")


def test_tablespec_name_starts_with_dot_rejected() -> None:
    with pytest.raises(ValidationError, match="schema.table"):
        TableSpec(name=".t_x")


def test_tablespec_name_ends_with_dot_rejected() -> None:
    with pytest.raises(ValidationError, match="schema.table"):
        TableSpec(name="analytics.")


def test_tablespec_with_columns() -> None:
    t = TableSpec(
        name="analytics.t_x",
        description="测试表",
        columns={
            "ip": ColumnSpec(description="IP 地址", example_values=["1.2.3.4"]),
            "event_date": ColumnSpec(description="日期"),
        },
    )
    assert t.columns["ip"].example_values == ["1.2.3.4"]
    assert t.columns["event_date"].description == "日期"
    assert t.columns["event_date"].example_values is None


def test_appconfig_tables_default_empty() -> None:
    cfg = AppConfig.model_validate({
        "database": {"host": "h", "dbname": "d", "user": "u"},
        "server": {"auth_token": "t"},
    })
    assert cfg.tables == []
    assert cfg.allowed_table_names() == set()


def test_appconfig_allowed_table_names() -> None:
    """两段式条目用 database.dbname 补全前缀，三段式条目原样，统一归一成三段式键。"""
    cfg = AppConfig.model_validate({
        "database": {"host": "h", "dbname": "D", "user": "u"},
        "server": {"auth_token": "t"},
        "tables": [
            {"name": "ANALYTICS.t_a"},          # 两段 → 补 dbname 前缀
            {"name": "Core.t_b"},               # 两段 → 补 dbname 前缀
            {"name": "OtherDB.public.t_c"},     # 三段 → 原样
        ],
    })
    assert cfg.allowed_table_names() == {
        "d.analytics.t_a",
        "d.core.t_b",
        "otherdb.public.t_c",
    }


def test_appconfig_allowed_table_names_cached(monkeypatch) -> None:
    """S-6：allowed_table_names_set 是 cached_property，第二次访问不重算。"""
    cfg = AppConfig.model_validate({
        "database": {"host": "h", "dbname": "d", "user": "u"},
        "server": {"auth_token": "t"},
        "tables": [{"name": "analytics.t_a"}, {"name": "analytics.t_b"}],
    })
    first = cfg.allowed_table_names_set
    second = cfg.allowed_table_names_set
    # cached_property 的标志：两次访问返回**同一个对象**
    assert first is second
    assert first == frozenset({"d.analytics.t_a", "d.analytics.t_b"})


def test_load_config_with_tables(tmp_path: Path) -> None:
    yaml_text = textwrap.dedent(
        """\
        database:
          host: h
          dbname: d
          user: u
        server:
          auth_token: t
        tables:
          - name: analytics.events
            description: "用户行为流水"
            columns:
              ip:
                description: "客户端 IP"
                example_values: ["1.2.3.4"]
        """
    )
    p = tmp_path / "config.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(p)

    assert len(cfg.tables) == 1
    t = cfg.tables[0]
    assert t.name == "analytics.events"
    assert t.description == "用户行为流水"
    assert t.columns["ip"].example_values == ["1.2.3.4"]
