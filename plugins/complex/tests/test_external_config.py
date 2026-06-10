"""complex 插件自有配置加载（_config）的离线测试。

覆盖：解析优先级（显式 path / env var / 默认路径 / 都没有报错）、sql 与 sql_file 互斥、
sql_file 相对配置目录解析、以及 register 把解析出的 SQL 透传给 ctx.aexecute。
全程不连 DB。
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any, Callable

import pytest

from redshift_mcp.config import AppConfig
from redshift_mcp.plugin import PluginContext
from redshift_mcp_complex import _config
from redshift_mcp_complex._config import ErrorApiConfig, load_resolved_sql

_LOG = logging.getLogger("redshift_mcp.plugins.complex")
_NAME = "complex"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """每条用例默认清掉 env var，避免开发机上设过的值干扰。"""
    monkeypatch.delenv(_config._ENV_VAR, raising=False)


def test_inline_sql(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('sql: "SELECT 42"\n', encoding="utf-8")
    assert load_resolved_sql(_LOG, _NAME, cfg) == "SELECT 42"


def test_sql_file_relative_to_config(tmp_path):
    (tmp_path / "q.sql").write_text("SELECT 7\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sql_file: q.sql\n", encoding="utf-8")
    assert load_resolved_sql(_LOG, _NAME, cfg).strip() == "SELECT 7"


def test_sql_and_sql_file_conflict():
    with pytest.raises(ValueError):
        ErrorApiConfig.model_validate({"sql": "SELECT 1", "sql_file": "q.sql"})


def test_neither_sql_nor_sql_file():
    with pytest.raises(ValueError):
        ErrorApiConfig.model_validate({})


def test_missing_sql_file(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sql_file: nope.sql\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_resolved_sql(_LOG, _NAME, cfg)


def test_env_var_takes_precedence_over_default(tmp_path, monkeypatch):
    env_cfg = tmp_path / "env.yaml"
    env_cfg.write_text('sql: "SELECT 99"\n', encoding="utf-8")
    monkeypatch.setenv(_config._ENV_VAR, str(env_cfg))
    assert load_resolved_sql(_LOG, _NAME) == "SELECT 99"   # 不传 path → 命中 env var


def test_env_var_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(_config._ENV_VAR, str(tmp_path / "ghost.yaml"))
    with pytest.raises(FileNotFoundError):
        load_resolved_sql(_LOG, _NAME)


def test_no_config_anywhere_raises(tmp_path, monkeypatch):
    # 默认路径指到不存在的文件、env var 已清 → 应抛带修复指引的错误。
    monkeypatch.setattr(_config, "_DEFAULT", tmp_path / "nope" / "config.yaml")
    with pytest.raises(FileNotFoundError) as excinfo:
        load_resolved_sql(_LOG, _NAME)
    msg = str(excinfo.value)
    assert "未找到配置" in msg
    assert _config._ENV_VAR in msg
    assert _NAME in msg   # 错误前缀用 plugin_name，非硬编码


# --- register 接线：解析出的 SQL 应透传给 ctx.aexecute ---

class _CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


async def test_register_passes_resolved_sql_to_aexecute(tmp_path, monkeypatch):
    import redshift_mcp_complex as pkg

    cfg = tmp_path / "config.yaml"
    cfg.write_text('sql: "SELECT 123"\n', encoding="utf-8")
    monkeypatch.setenv(_config._ENV_VAR, str(cfg))

    recorded: dict[str, Any] = {}

    async def _fake_aexecute(sql, params=None, *, max_rows, source=None):
        recorded["sql"] = sql
        recorded["params"] = params
        recorded["max_rows"] = max_rows
        recorded["source"] = source
        return {"count": 0, "truncated": False, "columns": [], "rows": []}

    appcfg = AppConfig.model_validate(
        {
            "database": {"host": "h", "dbname": "d", "user": "u"},
            "server": {"auth_token": "t"},
            "query": {"max_rows": 100},
        }
    )
    ctx = PluginContext(
        mcp=_CapturingMCP(),
        config=appcfg,
        logger=logging.getLogger("redshift_mcp.plugins"),
        sql_audit_logger=logging.getLogger("redshift_mcp.sql_audit"),
        request_id_var=contextvars.ContextVar("rid", default="-"),
        get_pool=lambda: object(),
        aexecute=_fake_aexecute,
        plugin_name=_NAME,
    )
    pkg.register(ctx)
    tool = ctx.mcp.tools["query_error_api_by_date"]

    result = await tool("2026-05-20")
    assert recorded["sql"] == "SELECT 123"
    assert recorded["params"] == {"event_date": "2026-05-20", "limit": 101}
    assert recorded["source"] == "plugin:complex"
    # 工具直接返回 db.aexecute 的 dict（已去掉 date 字段）。
    assert result == {"count": 0, "truncated": False, "columns": [], "rows": []}
