"""测试 dictConfig 构建器和 request_id 管线。"""
from __future__ import annotations

import json
import logging
import logging.config
from pathlib import Path

import pytest

from redshift_mcp.config import LoggingConfig
from redshift_mcp.middleware import RequestIdFilter, request_id_var, session_id_var
from redshift_mcp.server import build_log_config

_FULL_SID = "0123456789abcdef0123456789abcdef"  # 32 hex（uuid4().hex 形态）
_SHORT_SID = "01234567"  # 前 8 位


def test_stderr_only_mode_has_no_file_handler() -> None:
    cfg = LoggingConfig(level="INFO", file=None)
    conf = build_log_config(cfg)
    assert "file" not in conf["handlers"]
    assert "stderr" in conf["handlers"]


def test_file_mode_adds_rotating_file_handler(tmp_path: Path) -> None:
    log_file = tmp_path / "deep" / "nested" / "app.log"
    cfg = LoggingConfig(
        level="DEBUG", file=str(log_file), max_bytes=2048, backup_count=3
    )
    conf = build_log_config(cfg)
    assert conf["handlers"]["file"]["class"] == "logging.handlers.RotatingFileHandler"
    assert conf["handlers"]["file"]["filename"] == str(log_file)
    assert conf["handlers"]["file"]["maxBytes"] == 2048
    assert conf["handlers"]["file"]["backupCount"] == 3
    # build_log_config 必须自动创建父目录
    assert log_file.parent.exists()


def test_json_mode_selects_json_formatter() -> None:
    conf = build_log_config(LoggingConfig(level="INFO", file=None, as_json=True))
    assert conf["handlers"]["stderr"]["formatter"] == "json"


def test_text_mode_format_string_includes_rid() -> None:
    conf = build_log_config(LoggingConfig(level="INFO", file=None, as_json=False))
    assert "%(request_id)s" in conf["formatters"]["text"]["format"]


def test_request_id_filter_injects_default_dash() -> None:
    f = RequestIdFilter()
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="hi", args=(), exc_info=None,
    )
    f.filter(record)
    assert record.request_id == "-"


def test_request_id_filter_picks_up_contextvar_value() -> None:
    f = RequestIdFilter()
    rtoken = request_id_var.set("ab12cd34")
    stoken = session_id_var.set(_FULL_SID)
    try:
        record = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="hi", args=(), exc_info=None,
        )
        f.filter(record)
        assert record.request_id == "ab12cd34"
        # rid 与 sid 由同一 filter 同时设置（成对保证）；sid 渲染为前 8 位
        assert record.session_id == _SHORT_SID
    finally:
        session_id_var.reset(stoken)
        request_id_var.reset(rtoken)


def test_filter_extracts_session_id_from_sdk_message() -> None:
    """session_id_var 为 '-'，但记录来自 SDK streamable_http* logger 且消息内嵌 32hex id
    → filter 兜底抽出（修复 `Created new transport` 行 sid=- 的关键）。"""
    f = RequestIdFilter()
    record = logging.LogRecord(
        name="mcp.server.streamable_http_manager", level=logging.INFO, pathname="", lineno=0,
        msg="Created new transport with session ID: %s", args=(_FULL_SID,), exc_info=None,
    )
    f.filter(record)
    assert record.session_id == _SHORT_SID


def test_filter_no_session_fallback_for_non_sdk_logger() -> None:
    """非 SDK logger 即便消息里含 32hex 也不兜底抽取（避免误匹配）。"""
    f = RequestIdFilter()
    record = logging.LogRecord(
        name="redshift_mcp", level=logging.INFO, pathname="", lineno=0,
        msg="incidental hex %s here", args=(_FULL_SID,), exc_info=None,
    )
    f.filter(record)
    assert record.session_id == "-"


def test_logging_pipeline_writes_rid_to_file(tmp_path: Path) -> None:
    """套上真实 dictConfig，验证 request_id 能进到文件 handler 输出里。"""
    log_file = tmp_path / "out.log"
    cfg = LoggingConfig(level="DEBUG", file=str(log_file), as_json=False)
    logging.config.dictConfig(build_log_config(cfg))

    log = logging.getLogger("redshift_mcp.test_pipeline")
    token = request_id_var.set("trace1234")
    stoken = session_id_var.set(_FULL_SID)
    try:
        log.info("hello-with-rid")
    finally:
        session_id_var.reset(stoken)
        request_id_var.reset(token)
    log.info("hello-no-rid")

    # Flush + detach handler，确保文件已落盘且不被锁住。
    for name in ("redshift_mcp", "root"):
        for h in list(logging.getLogger(name).handlers):
            try:
                h.flush()
            except Exception:
                pass

    content = log_file.read_text(encoding="utf-8")
    # rid 与 sid 成对出现；sid 渲染为前 8 位
    assert f"[rid=trace1234 sid={_SHORT_SID}] hello-with-rid" in content
    assert "[rid=- sid=-] hello-no-rid" in content


def test_logging_pipeline_json_mode(tmp_path: Path) -> None:
    log_file = tmp_path / "out.json.log"
    cfg = LoggingConfig(level="INFO", file=str(log_file), as_json=True)
    logging.config.dictConfig(build_log_config(cfg))

    log = logging.getLogger("redshift_mcp.test_json")
    token = request_id_var.set("jsonrid1")
    stoken = session_id_var.set(_FULL_SID)
    try:
        log.info("a json line")
    finally:
        session_id_var.reset(stoken)
        request_id_var.reset(token)

    for name in ("redshift_mcp", "root"):
        for h in list(logging.getLogger(name).handlers):
            try:
                h.flush()
            except Exception:
                pass

    last_line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    obj = json.loads(last_line)
    assert obj["request_id"] == "jsonrid1"
    assert obj["session_id"] == _SHORT_SID
    assert obj["msg"] == "a json line"
    assert obj["level"] == "INFO"


def test_main_startup_log_uses_as_json_field() -> None:
    """回归守护：server.main() 启动那条 `日志配置完成: ... json=%s` 必须引用
    LoggingConfig.as_json，不能退回到 .json —— 后者是 pydantic v2 BaseModel
    上 deprecated 的 .json() 方法对象，%s 格式化后会渲染成
    `<bound method BaseModel.json of LoggingConfig(...)>` 而非 True/False。
    """
    import inspect

    from redshift_mcp import server

    src = inspect.getsource(server.main)
    assert "_cfg.logging.as_json" in src, "启动日志必须引用 _cfg.logging.as_json"

    # 行为侧验证：用与 server.main 同样的格式化模板,确认 as_json 渲染为 True/False
    cfg = LoggingConfig(level="INFO", file=None, as_json=True)
    rendered_ok = "日志配置完成: level=%s file=%s json=%s" % (
        cfg.level, cfg.file or "(stderr only)", cfg.as_json,
    )
    assert "json=True" in rendered_ok
    assert "bound method" not in rendered_ok

    # 反例：直接引用 cfg.json（即修复前的 bug 写法）必然渲染成 bound method。
    # 这条断言一旦失败（例如 pydantic 彻底移除 .json），意味着源码再写错也不会
    # 静默渲染成方法 repr —— 那时本测试可连同 fix 一起移除。
    rendered_buggy = "json=%s" % (cfg.json,)
    assert "bound method" in rendered_buggy


# ===== SQL 审计子 logger（sql_audit_level / sql_audit_file）=====


def test_sql_audit_logger_registered_in_dictconfig() -> None:
    """sql_audit logger 应被 build_log_config 注册到 dictConfig 的 loggers 段。"""
    conf = build_log_config(LoggingConfig(level="INFO"))
    assert "redshift_mcp.sql_audit" in conf["loggers"]


def test_sql_audit_default_level_is_warning() -> None:
    """默认 sql_audit_level=WARNING（PII 安全 —— INFO/DEBUG 级 SQL 不输出）。"""
    conf = build_log_config(LoggingConfig(level="INFO"))
    assert conf["loggers"]["redshift_mcp.sql_audit"]["level"] == "WARNING"


def test_sql_audit_propagate_false() -> None:
    """sql_audit 不应向父 logger redshift_mcp 冒泡（否则会重复输出）。"""
    conf = build_log_config(LoggingConfig(level="INFO"))
    assert conf["loggers"]["redshift_mcp.sql_audit"]["propagate"] is False


def test_merged_mode_handler_level_takes_min() -> None:
    """合流模式：handler.level = min(level, sql_audit_level)。

    用例：level=WARNING + sql_audit_level=INFO → handler 取 INFO，
    让 sql_audit INFO 记录能穿过 handler 这道闸门。
    """
    conf = build_log_config(LoggingConfig(level="WARNING", sql_audit_level="INFO"))
    assert conf["handlers"]["stderr"]["level"] == "INFO"


def test_merged_mode_handler_level_unchanged_when_audit_stricter() -> None:
    """合流模式 + audit 比 main 严格：handler 保持 main 的 level。"""
    conf = build_log_config(LoggingConfig(level="INFO", sql_audit_level="WARNING"))
    assert conf["handlers"]["stderr"]["level"] == "INFO"


def test_merged_mode_uses_main_handlers() -> None:
    """合流模式下，sql_audit 共用 main 的 handler 列表。"""
    conf = build_log_config(LoggingConfig(level="INFO", file="/tmp/x.log"))
    audit_handlers = conf["loggers"]["redshift_mcp.sql_audit"]["handlers"]
    main_handlers = conf["loggers"]["redshift_mcp"]["handlers"]
    assert audit_handlers == main_handlers
    assert "stderr_audit" not in conf["handlers"]
    assert "file_audit" not in conf["handlers"]


def test_standalone_mode_has_dedicated_handlers(tmp_path: Path) -> None:
    """sql_audit_file 非空 → audit 独占 stderr_audit + file_audit handler。"""
    audit_file = tmp_path / "sql-audit.log"
    conf = build_log_config(LoggingConfig(
        level="INFO",
        sql_audit_level="INFO",
        sql_audit_file=str(audit_file),
    ))
    # 独立 handler 已注册
    assert "stderr_audit" in conf["handlers"]
    assert "file_audit" in conf["handlers"]
    # audit logger 用独立 handler
    assert conf["loggers"]["redshift_mcp.sql_audit"]["handlers"] == ["stderr_audit", "file_audit"]
    # 主 logger 仍只用 main handler
    assert "stderr_audit" not in conf["loggers"]["redshift_mcp"]["handlers"]
    # 独立模式下 main handler level 不再放宽（无需联动）
    assert conf["handlers"]["stderr"]["level"] == "INFO"


def test_standalone_mode_audit_handler_level_follows_sql_audit_level(tmp_path: Path) -> None:
    """独立文件模式：audit handler level 直接 = sql_audit_level。"""
    conf = build_log_config(LoggingConfig(
        level="ERROR",
        sql_audit_level="DEBUG",
        sql_audit_file=str(tmp_path / "audit.log"),
    ))
    assert conf["handlers"]["stderr_audit"]["level"] == "DEBUG"
    assert conf["handlers"]["file_audit"]["level"] == "DEBUG"
    # main handler 仍受 main level 约束
    assert conf["handlers"]["stderr"]["level"] == "ERROR"


def test_standalone_mode_auto_creates_audit_parent_dir(tmp_path: Path) -> None:
    """sql_audit_file 父目录应被自动创建（与 file 一致的行为）。"""
    deep = tmp_path / "deep" / "nested" / "audit.log"
    build_log_config(LoggingConfig(
        level="INFO",
        sql_audit_level="INFO",
        sql_audit_file=str(deep),
    ))
    assert deep.parent.exists()


def test_sql_audit_end_to_end_writes_to_standalone_file(tmp_path: Path) -> None:
    """独立模式 end-to-end：sql_audit_logger.info() 真实写到 audit 文件，
    且不污染 main 运行日志文件。"""
    main_file = tmp_path / "main.log"
    audit_file = tmp_path / "audit.log"
    cfg = LoggingConfig(
        level="INFO",
        file=str(main_file),
        sql_audit_level="INFO",
        sql_audit_file=str(audit_file),
        as_json=False,
    )
    logging.config.dictConfig(build_log_config(cfg))

    main = logging.getLogger("redshift_mcp")
    audit = logging.getLogger("redshift_mcp.sql_audit")

    token = request_id_var.set("trace42")
    try:
        main.info("普通运行日志")
        audit.info("SQL: SELECT * FROM analytics.users WHERE email='x@y.com'")
    finally:
        request_id_var.reset(token)

    for h in list(logging.getLogger("redshift_mcp").handlers) + \
             list(logging.getLogger("redshift_mcp.sql_audit").handlers):
        try: h.flush()
        except Exception: pass

    main_content = main_file.read_text(encoding="utf-8")
    audit_content = audit_file.read_text(encoding="utf-8")

    # 运行日志只含普通信息，不含 SQL 文本（PII 隔离）
    assert "普通运行日志" in main_content
    assert "SELECT" not in main_content
    assert "x@y.com" not in main_content

    # 审计文件只含 SQL，不含运行日志噪音
    assert "SELECT * FROM analytics.users" in audit_content
    assert "trace42" in audit_content   # request_id filter 也作用于 audit handler
    assert "普通运行日志" not in audit_content


def test_sql_audit_filtered_at_default_warning(tmp_path: Path) -> None:
    """默认 sql_audit_level=WARNING：audit.info() 调用被过滤，不写文件。"""
    audit_file = tmp_path / "audit.log"
    cfg = LoggingConfig(
        level="INFO",
        sql_audit_file=str(audit_file),
        # sql_audit_level 默认 WARNING
    )
    logging.config.dictConfig(build_log_config(cfg))

    audit = logging.getLogger("redshift_mcp.sql_audit")
    audit.info("SQL: SELECT 1")  # INFO < WARNING，应被过滤

    for h in list(audit.handlers):
        try: h.flush()
        except Exception: pass

    # audit_file 文件可能根本没创建（写入未触发）；存在则不应含 SELECT
    if audit_file.exists():
        assert "SELECT 1" not in audit_file.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_logging():
    """每个 test 都会重新 apply dictConfig；测试之间 detach handler，
    并重置 level / propagate，防止 dictConfig 留下的状态污染其他测试文件
    （例如 propagate=False 会让其他文件的 caplog 抓不到子 logger 记录）。"""
    yield
    for name in (
        "redshift_mcp",
        "redshift_mcp.sql_audit",   # SQL 审计子 logger
        "redshift_mcp.plugins",     # 插件子 logger 子树（兜底，防未来插件改 level/propagate）
        "redshift_mcp.plugins.sql_tools",  # 声明式 SQL 工具子 logger（显式兜底）
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "mcp",
        "",
    ):
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            try:
                h.close()
            except Exception:
                pass
            logger.removeHandler(h)
        # 重置 level 和 propagate 为 stdlib 默认值，避免 dictConfig 副作用
        # 跨文件污染（root logger 保持 WARNING 为 Python 默认）。
        if name == "":
            logger.setLevel(logging.WARNING)
        else:
            logger.setLevel(logging.NOTSET)   # 恢复"继承父 logger 级别"
        logger.propagate = True
