"""load_config 的 include 合并 + sql_file 外链测试 —— 全离线，用 tmp_path 造文件。"""
from __future__ import annotations

from pathlib import Path

import pytest

from redshift_mcp.config import load_config

_MAIN = """\
database: {host: h, dbname: d, user: u}
server: {auth_token: t}
query: {max_rows: 100}
include:
  - conf.d/*.yaml
sql_tools:
  - name: inline_tool
    description: 内联工具
    sql: "SELECT 1 AS x LIMIT 1"
"""


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _setup(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    _write(cfg, _MAIN)
    _write(tmp_path / "conf.d" / "tables.yaml", "tables:\n  - name: analytics.t_one\n")
    _write(
        tmp_path / "conf.d" / "sql_tools.yaml",
        "sql_tools:\n"
        "  - name: frag_tool\n"
        "    description: 片段工具\n"
        "    sql_file: ../queries/frag.sql\n"
        "    params:\n"
        "      - {name: date, type: date}\n",
    )
    _write(tmp_path / "queries" / "frag.sql",
           "SELECT * FROM analytics.t WHERE event_date = %(date)s LIMIT 10\n")
    return cfg


def test_include_merges_and_appends_lists(tmp_path: Path) -> None:
    cfg = load_config(_setup(tmp_path))
    names = {t.name for t in cfg.sql_tools}
    assert names == {"inline_tool", "frag_tool"}     # 主配置 + 片段列表追加
    assert [t.name for t in cfg.tables] == ["analytics.t_one"]


def test_sql_file_inlined_relative_to_fragment_dir(tmp_path: Path) -> None:
    cfg = load_config(_setup(tmp_path))
    frag = next(t for t in cfg.sql_tools if t.name == "frag_tool")
    assert "event_date = %(date)s" in frag.sql           # sql_file 已内联
    assert "LIMIT 10" in frag.sql


def test_scalar_in_fragment_overrides_main(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write(cfg, _MAIN)
    _write(tmp_path / "conf.d" / "override.yaml", "query: {max_rows: 5}\n")
    assert load_config(cfg).query.max_rows == 5      # 片段标量覆盖主配置（深合并）


def test_nested_include_in_fragment_ignored(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write(cfg, _MAIN)
    # 片段自己又写 include —— 应被忽略。sneaky.yaml 放在 conf.d 之外，
    # 主配置的 conf.d/*.yaml 不会直接匹配到它，只有通过片段的嵌套 include 才会引入。
    _write(tmp_path / "conf.d" / "frag.yaml", "include: ['../sneaky.yaml']\ntables: []\n")
    _write(tmp_path / "sneaky.yaml",
           "sql_tools:\n  - {name: sneaky, description: x, sql: 'SELECT 1'}\n")
    names = {t.name for t in load_config(cfg).sql_tools}
    assert "sneaky" not in names


def test_sql_and_sql_file_both_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write(cfg,
           "database: {host: h, dbname: d, user: u}\n"
           "server: {auth_token: t}\n"
           "sql_tools:\n"
           "  - name: bad\n    description: x\n    sql: 'SELECT 1'\n    sql_file: q.sql\n")
    _write(tmp_path / "q.sql", "SELECT 1")
    with pytest.raises(ValueError, match="不能同时配置 sql 和 sql_file"):
        load_config(cfg)


def test_sql_file_missing_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write(cfg,
           "database: {host: h, dbname: d, user: u}\n"
           "server: {auth_token: t}\n"
           "sql_tools:\n  - name: bad\n    description: x\n    sql_file: nope.sql\n")
    with pytest.raises(FileNotFoundError):
        load_config(cfg)


def test_no_include_still_loads(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write(cfg, "database: {host: h, dbname: d, user: u}\nserver: {auth_token: t}\n")
    assert load_config(cfg).sql_tools == []
