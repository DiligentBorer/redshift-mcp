"""error_api 插件的自有配置加载（约定 + env var；结构与 host config.yaml 同构）。

这是「插件如何自带 gitignored 外部配置」的参考实现 —— 插件配置内聚原则不变（host
config.yaml 不承载、PluginContext 不透传），插件自行按以下优先级解析配置：

1. 显式传入的 ``path``（测试用）；
2. 环境变量 ``REDSHIFT_MCP_ERROR_API_CONFIG`` 指向的文件（不重新 build wheel 就想换配置时用）；
3. 默认约定：插件包目录内的 ``config.yaml``（``Path(__file__).parent / "config.yaml"``）——
   dev/workspace 下是源码树里开发者自建的（gitignored）；生产下是 build 时打进 wheel 的真实配置
   （默认路径即命中、无需 env var）。

都找不到 → 抛 ``FileNotFoundError``（含期望路径 + 修复指引），**不回落范本**；由
``register`` 上层的 ``load_plugins`` try/except 隔离、记日志、跳过本插件注册，不搞崩 server。

模板见仓库的 ``config.example.yaml`` / ``queries/error_api.example.sql``（仅在 git、不进生产
wheel），供拷贝。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator

_ENV_VAR = "REDSHIFT_MCP_ERROR_API_CONFIG"
_PKG_DIR = Path(__file__).resolve().parent
_DEFAULT = _PKG_DIR / "config.yaml"


class ErrorApiConfig(BaseModel):
    """error_api 插件的私有配置模型（结构与 host config.yaml 同构，可按需扩展业务参数）。

    ``sql`` 与 ``sql_file`` 二选一：``sql`` 直接内联；``sql_file`` 相对配置文件所在目录读取。
    """

    sql: str | None = None
    sql_file: str | None = None

    @model_validator(mode="after")
    def _exactly_one_sql_source(self) -> ErrorApiConfig:
        """强制 sql / sql_file 恰好配置其一 —— 配置存在就必须给出查询 SQL。"""
        has_sql = bool(self.sql and self.sql.strip())
        has_file = bool(self.sql_file and self.sql_file.strip())
        if has_sql and has_file:
            raise ValueError("不能同时配置 sql 和 sql_file（二选一）")
        if not has_sql and not has_file:
            raise ValueError("必须配置 sql 或 sql_file 之一（error_api 需要查询 SQL）")
        return self


def _resolve_path(path: str | Path | None) -> Path:
    """按 显式 path > env var > 包内默认 优先级定位配置文件；都没有则抛带修复指引的错误。"""
    if path is not None:
        explicit = Path(path)
        if not explicit.is_file():
            raise FileNotFoundError(f"error_api 指定的配置文件不存在: {explicit}")
        return explicit

    env_path = os.environ.get(_ENV_VAR)
    if env_path:
        from_env = Path(env_path)
        if not from_env.is_file():
            raise FileNotFoundError(
                f"error_api 的 {_ENV_VAR} 指向的配置文件不存在: {from_env}"
            )
        return from_env

    if _DEFAULT.is_file():
        return _DEFAULT

    raise FileNotFoundError(
        f"error_api 未找到配置文件。请在 {_DEFAULT} 创建 config.yaml，"
        f"或设置环境变量 {_ENV_VAR} 指向配置文件路径；可参考插件仓库的 config.example.yaml。"
    )


def load_resolved_sql(logger: logging.Logger, path: str | Path | None = None) -> str:
    """加载并解析 error_api 配置，返回最终查询 SQL 文本。

    读取选中的 YAML → ``ErrorApiConfig`` 校验 → ``sql`` 内联 / ``sql_file`` 相对配置目录读取。
    找不到配置、校验失败或 sql_file 缺失时抛错（无运行期范本兜底）。
    """
    cfg_path = _resolve_path(path)
    logger.info("error_api 配置来源: %s", cfg_path)

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = ErrorApiConfig.model_validate(raw)

    if cfg.sql:
        return cfg.sql

    # sql_file 相对配置文件所在目录解析（与 host _inline_sql_file 语义一致）。
    sql_path = (cfg_path.parent / cfg.sql_file).resolve()
    if not sql_path.is_file():
        raise FileNotFoundError(f"error_api 的 sql_file 不存在: {sql_path}")
    return sql_path.read_text(encoding="utf-8")
