from __future__ import annotations

import argparse
import atexit
import contextvars
import hmac
import json
import logging
import logging.config
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, db, sql_guard
from .config import AppConfig, LoggingConfig, load_config
# "DB / 运行时错误"分类元组移到零依赖叶子模块 errors.py，让插件也能共享
# （见 errors.py / plugin.py）。工具 @mcp.tool 路径用它把这类错误包装成带 rid 的
# RuntimeError 抛给客户端，但**不**吞掉编程错误（TypeError / KeyError / sqlglot
# 内部断言等）—— 让那些 bug 类异常原样冒泡，由 FastMCP 包成 500，便于早暴露。
from .errors import DB_RUNTIME_ERRORS as _DB_RUNTIME_ERRORS
from .plugin import PluginContext, load_plugins

logger = logging.getLogger("redshift_mcp")
# SQL 审计专用子 logger（见 db.py 顶部说明）。失败路径也走它，让"失败的 SQL"
# 不会因 logger.error 一并进运行日志。
sql_audit_logger = logging.getLogger("redshift_mcp.sql_audit")

# 每请求关联 id。由 RequestIdMiddleware 设置；非请求上下文中默认为 "-"。
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def new_request_id() -> str:
    """生成 8 位 hex 字符串（~32 bit），单日志窗口内做 grep 关联足够。"""
    return secrets.token_hex(4)


class RequestIdFilter(logging.Filter):
    """把当前 request_id 注入到每条 LogRecord，便于 formatter 渲染。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """精简 JSON 行 formatter，无额外依赖。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# Python logging level 名 → 数值映射，用于本模块内部比较取 min。
# 标准库 ``logging.getLevelNamesMapping()`` 仅 3.11+ 才有；这里手写避免依赖。
_LOG_LEVEL_INT: dict[str, int] = {
    "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50,
}


def _min_level_name(*names: str) -> str:
    """返回数值最小（即最宽松）的那个 level 名。"""
    return min(names, key=lambda n: _LOG_LEVEL_INT[n])


def build_log_config(cfg: LoggingConfig) -> dict[str, Any]:
    """返回一份 dictConfig：

    - 始终输出到 stderr
    - 当 cfg.file 不为空时，附加一个滚动文件 handler
    - 给每个 handler 都挂上 request_id filter
    - 把 uvicorn 的几个 logger 也接到同一组 handler 上
    - ``redshift_mcp.sql_audit`` 子 logger 有两种部署模式：
      * cfg.sql_audit_file 为 None：与 main 合流，共享 stderr + file handler；
        handler.level 自动取 ``min(cfg.level, cfg.sql_audit_level)``，让 audit
        记录无论 main level 多严格都能穿过 handler。
      * cfg.sql_audit_file 非空：audit 独占 stderr_audit + file_audit handler，
        与 main 完全分离 —— 不需要 handler level 联动，audit 文件可独立做
        retention / 加密 / SIEM 接入。
    """
    formatter_key = "json" if cfg.as_json else "text"
    formatters: dict[str, dict[str, Any]] = {
        "text": {
            "format": "%(asctime)s %(levelname)s [%(name)s] [rid=%(request_id)s] %(message)s",
        },
        "json": {
            "()": "redshift_mcp.server.JsonFormatter",
        },
    }

    audit_standalone = cfg.sql_audit_file is not None
    # 合流模式下，主 handler 的 level 要取 min(level, sql_audit_level)，
    # 否则 sql_audit logger 放行的低级记录会被 handler 二次过滤掉。
    main_handler_level = (
        cfg.level if audit_standalone else _min_level_name(cfg.level, cfg.sql_audit_level)
    )

    handlers: dict[str, dict[str, Any]] = {
        "stderr": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": formatter_key,
            "filters": ["request_id"],
            "level": main_handler_level,
        },
    }

    if cfg.file:
        Path(cfg.file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(Path(cfg.file).expanduser()),
            "maxBytes": cfg.max_bytes,
            "backupCount": cfg.backup_count,
            "encoding": "utf-8",
            "formatter": formatter_key,
            "filters": ["request_id"],
            "level": main_handler_level,
        }

    main_handler_names = list(handlers.keys())

    # 独立模式：给 sql_audit 单独建 handler，level 由 sql_audit_level 直接控制
    if audit_standalone:
        handlers["stderr_audit"] = {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": formatter_key,
            "filters": ["request_id"],
            "level": cfg.sql_audit_level,
        }
        Path(cfg.sql_audit_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        handlers["file_audit"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(Path(cfg.sql_audit_file).expanduser()),
            "maxBytes": cfg.max_bytes,
            "backupCount": cfg.backup_count,
            "encoding": "utf-8",
            "formatter": formatter_key,
            "filters": ["request_id"],
            "level": cfg.sql_audit_level,
        }
        audit_handler_names = ["stderr_audit", "file_audit"]
    else:
        audit_handler_names = main_handler_names

    loggers_cfg = {
        # 应用自身的 logger 树
        "redshift_mcp": {"level": cfg.level, "handlers": main_handler_names, "propagate": False},
        # SQL 审计专用子 logger
        "redshift_mcp.sql_audit": {
            "level": cfg.sql_audit_level,
            "handlers": audit_handler_names,
            "propagate": False,
        },
        # uvicorn 的三个 logger
        "uvicorn": {"level": cfg.level, "handlers": main_handler_names, "propagate": False},
        "uvicorn.error": {"level": cfg.level, "handlers": main_handler_names, "propagate": False},
        "uvicorn.access": {"level": cfg.level, "handlers": main_handler_names, "propagate": False},
        # mcp / FastMCP 内部 logger
        "mcp": {"level": cfg.level, "handlers": main_handler_names, "propagate": False},
    }

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {"()": "redshift_mcp.server.RequestIdFilter"},
        },
        "formatters": formatters,
        "handlers": handlers,
        "loggers": loggers_cfg,
        "root": {"level": cfg.level, "handlers": main_handler_names},
    }


_cfg: AppConfig | None = None
mcp = FastMCP("redshift-mcp")
# FastMCP 的构造器目前尚未暴露 `version` 参数；底层 lowlevel Server 在
# `.version` 为 None 时会回退到 importlib.metadata.version("mcp")，这就是
# 之前 Inspector 显示的是 MCP SDK 版本号的原因。在此显式设置，让客户端
# 通过 `serverInfo.version` 看到的是「应用自身」的版本。
mcp._mcp_server.version = __version__


def _get_cfg() -> AppConfig:
    if _cfg is None:
        raise RuntimeError("应用配置未初始化")
    return _cfg


@mcp.tool()
def list_tables() -> list[dict[str, Any]]:
    """列出本 server 配置中允许查询的全部 Redshift 表。

    返回每张表的 schema-qualified 名字与可选中文描述。在调用
    ``describe_table`` 或 ``run_sql`` 之前应先调本工具发现可用表 ——
    白名单不在结果里的表无法访问。

    Returns:
        ``[{"name": "schema.table", "description": str | None}, ...]``；
        若白名单为空则返回 ``[]``，此时 ``describe_table`` / ``run_sql``
        全部会拒绝。
    """
    cfg = _get_cfg()
    return [
        {"name": t.name, "description": t.description}
        for t in cfg.tables
    ]


@mcp.tool()
def describe_table(table: str) -> dict[str, Any]:
    """查指定表的列、类型与可选补充说明。

    入参 ``table`` 必须是 schema-qualified 全名（如
    ``dwd.t_action_info_widen``），且必须先在 ``list_tables`` 返回的
    白名单中出现，否则拒绝。

    列信息从 Redshift ``SVV_COLUMNS`` 实时拉取，并叠加 config 里同名列
    的 ``description`` / ``example_values`` 提示（如有配置）。

    Returns:
        ``{name, description, columns: [{name, type, ordinal_position,
        description?, example_values?}], row_count_estimate?}``
    """
    if not isinstance(table, str) or "." not in table:
        raise ValueError(
            f"表名必须是 schema.table 格式: {table!r}。"
            f"请先调用 list_tables 查看可用表全名。"
        )
    table_norm = table.lower()
    cfg = _get_cfg()
    if table_norm not in cfg.allowed_table_names():
        raise ValueError(
            f"表 {table_norm!r} 不在白名单内。"
            f"请先调用 list_tables 查看可用表全名。"
        )

    spec = next((t for t in cfg.tables if t.name == table_norm), None)
    schema, tname = table_norm.split(".", 1)

    rid = request_id_var.get()
    try:
        raw_columns = db.fetch_table_columns(schema, tname)
        table_info = db.fetch_table_info(schema, tname)
    except _DB_RUNTIME_ERRORS as exc:
        logger.exception("describe_table 失败 table=%s", table_norm)
        raise RuntimeError(
            f"describe_table 失败 (request_id={rid}, 详见服务端日志): "
            f"{exc.__class__.__name__}"
        ) from exc

    # 白名单内但 SVV_COLUMNS 查不到列 → 表可能不存在 / 是 view / 权限不足。
    # 明确报错好于返回 columns: []（后者会让 LLM 误以为该表无列）。
    if not raw_columns:
        raise ValueError(
            f"表 {table_norm!r} 在 SVV_COLUMNS 查不到任何列；可能不存在、"
            "已被删除、或当前 Redshift 账号无权访问。请调 list_tables 确认白名单。"
        )

    spec_cols = spec.columns if spec else {}
    enriched: list[dict[str, Any]] = []
    for col in raw_columns:
        cname = (col.get("name") or "").lower()
        merged = dict(col)
        if cname in spec_cols:
            extra = spec_cols[cname]
            if extra.description is not None:
                merged["description"] = extra.description
            if extra.example_values is not None:
                merged["example_values"] = extra.example_values
        enriched.append(merged)

    result: dict[str, Any] = {
        "name": table_norm,
        "description": spec.description if spec else None,
        "columns": enriched,
    }
    if table_info and table_info.get("row_count_estimate") is not None:
        result["row_count_estimate"] = table_info["row_count_estimate"]
    return result


@mcp.tool()
def run_sql(sql: str) -> dict[str, Any]:
    """执行单条 SELECT 并返回结果。

    所有引用的表都必须 schema-qualified（``schema.table``），且都在
    ``list_tables`` 白名单内；否则拒绝。仅允许 SELECT —— INSERT /
    UPDATE / DELETE / DROP / CREATE / ALTER / SET / 多语句 等都会被
    拒绝。返回结果按 ``query.max_rows`` 截断（``truncated=true`` 表示
    被截断）。建议先调 ``describe_table`` 了解列结构再写 SQL。

    Args:
        sql: 单条 SELECT 字符串。

    Returns:
        ``{count, truncated, columns: [...], rows: [{col: val, ...}, ...]}``
    """
    cfg = _get_cfg()
    rid = request_id_var.get()

    # SQL 安全校验失败 → ValueError 原样抛给客户端（含完整原因，便于 LLM 自我纠正），
    # 不带 rid（属于入参错误）。但同时记两条日志用于运维观测：
    #   - logger.info("run_sql 拒绝: <原因>")  进运行日志，含拒绝原因不含 SQL 全文（PII 安全）
    #   - sql_audit_logger.info("被拒绝的 SQL: <完整 SQL>") 走 audit 通道，
    #     默认 sql_audit_level=WARNING 时不输出，运维需要审计时切 INFO 才落盘
    try:
        ast = sql_guard.validate_select_only(sql, cfg.allowed_table_names())
    except ValueError as exc:
        logger.info("run_sql 拒绝: %s", exc)
        sql_audit_logger.info("被拒绝的 SQL: %s", sql)
        raise
    capped_sql = sql_guard.apply_row_cap(ast, cfg.query.max_rows)

    try:
        return db.query_sql(capped_sql, max_rows=cfg.query.max_rows)
    except _DB_RUNTIME_ERRORS as exc:
        # 完整 traceback（带 rid）通过 filter 写到运行日志。
        # SQL 文本本身可能含 PII（WHERE 子句里的邮箱/用户名等），不进运行
        # 日志，仅当 sql_audit_level 放宽时写到 audit 通道。
        logger.exception("run_sql 失败")
        sql_audit_logger.info("失败的 SQL: %s", capped_sql)
        raise RuntimeError(
            f"run_sql 失败 (request_id={rid}, 详见服务端日志): "
            f"{exc.__class__.__name__}"
        ) from exc


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个 HTTP 请求生成（或接受上游传入）一个 request_id，绑定到当前
    contextvars 上下文里，下游 logger / 工具处理器都能读到。
    响应里也会回写 ``X-Request-ID`` 头。
    """

    HEADER = "x-request-id"

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get(self.HEADER) or new_request_id()
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        return response


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, token: str, protected_path: str) -> None:
        super().__init__(app)
        self._token = token
        self._protected_path = protected_path.rstrip("/")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        protected = (
            path == self._protected_path
            or path.startswith(self._protected_path + "/")
        )
        if not protected:
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        scheme, _, presented = auth.partition(" ")
        if scheme.lower() != "bearer" or not presented:
            return JSONResponse(
                {"error": "缺少或格式错误的 Authorization 头"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not hmac.compare_digest(presented, self._token):
            return JSONResponse(
                {"error": "token 无效"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="redshift-mcp",
        description="MCP Server (Streamable HTTP) exposing Redshift Error API queries.",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=os.environ.get("REDSHIFT_MCP_CONFIG", "config.yaml"),
        help="Path to YAML config file (default: config.yaml or $REDSHIFT_MCP_CONFIG).",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="Override logging.level from the config (DEBUG/INFO/WARNING/...).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global _cfg

    args = _parse_args(argv)

    # 1) 先加载配置（若失败时，先用 stderr-only 的临时 logger 把错误打出来）
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        _cfg = load_config(args.config)
    except Exception as exc:
        logger.error("加载配置失败 path=%s: %s", args.config, exc)
        return 2

    if args.log_level:
        _cfg.logging.level = args.log_level.upper()  # type: ignore[assignment]

    # 2) 应用真正的日志配置（file + stderr + request_id filter）
    log_config = build_log_config(_cfg.logging)
    logging.config.dictConfig(log_config)
    logger.info(
        "日志配置完成: level=%s sql_audit_level=%s sql_audit_file=%s file=%s json=%s",
        _cfg.logging.level,
        _cfg.logging.sql_audit_level,
        _cfg.logging.sql_audit_file or "(merged)",
        _cfg.logging.file or "(stderr only)",
        _cfg.logging.as_json,
    )

    # 3) 初始化 DB 连接池
    try:
        db.init_pool(_cfg.database, _cfg.query.statement_timeout_ms)
    except Exception as exc:
        logger.error("初始化 Redshift 连接池失败: %s", exc)
        return 3
    atexit.register(db.close_pool)

    # 3.5) 加载外部插件（entry_points 发现）。放在 streamable_http_app() 之前，
    # 但 FastMCP 的 list_tools 是实时读取、不快照，这里注册的工具下一次 list_tools
    # 请求即可见。坏插件被加载器隔离，不影响 server 启动。
    if _cfg.plugins.enabled:
        plugin_ctx = PluginContext(
            mcp=mcp,
            config=_cfg,
            logger=logger,
            sql_audit_logger=sql_audit_logger,
            request_id_var=request_id_var,
            get_pool=db.get_pool,
        )
        loaded = load_plugins(plugin_ctx, disabled=_cfg.plugins.disabled)
        logger.info("插件注册完成: %s", loaded or "(无)")
    else:
        logger.info("插件加载已禁用 (plugins.enabled=false)")

    # 4) 构建 ASGI app 并挂中间件（rid 在外层、auth 在内层）。
    # Starlette 里**最后 add_middleware 的处于最外层**，因此顺序很关键：
    # 先 add auth，再 add request-id；这样 request-id 包住 auth，401 响应
    # 也带 X-Request-ID 头。
    mcp.settings.streamable_http_path = _cfg.server.path
    app = mcp.streamable_http_app()
    app.add_middleware(
        BearerAuthMiddleware,
        token=_cfg.server.auth_token,
        protected_path=_cfg.server.path,
    )
    app.add_middleware(RequestIdMiddleware)

    logger.info(
        "启动 redshift-mcp，监听 http://%s:%d%s",
        _cfg.server.host, _cfg.server.port, _cfg.server.path,
    )
    uvicorn.run(
        app,
        host=_cfg.server.host,
        port=_cfg.server.port,
        log_config=log_config,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
