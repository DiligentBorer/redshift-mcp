# Redshift MCP Server 多阶段镜像。
# 两线共用:公开线(GHCR)与私有线(自建 registry)都用本 Dockerfile,构建上下文 = 仓库根。
#
# 阶段:
#   builder  —— 用 uv 把 host + 所有 workspace 插件打成 wheel
#   test     —— 在容器内跑 pytest(链路环境 Docker 化的一环;CI 用 --target test 单独跑)
#   runtime  —— slim 运行时,只装 wheel,无源码、无 uv、无密钥

# ---------- builder ----------
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /src
COPY . /src
# --all-packages:产出 redshift_mcp(host) 与 redshift_mcp_sdk(契约层) 两个 wheel
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --all-packages -o /dist

# ---------- test ----------
FROM builder AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-packages \
    && uv run pytest -q

# ---------- runtime ----------
FROM python:3.13-slim AS runtime
# 镜像只装 host(通用服务器 + 插件框架),不夹带任何插件。
# 业务插件都是外部独立仓:在其仓构建 wheel,再以本镜像为基础 uv pip install 进 /app/.venv 即可
# （靠 entry_points 自动发现,无需改 host 配置）。
# 非 root 运行,贴合最小权限原则
RUN useradd -r -u 10001 -m -d /app appuser
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY --from=builder /dist /tmp/dist
# 在干净 venv 里装 wheel(非 editable),装完即删 uv 与 wheel,保持镜像精简无构建残留。
# glob redshift_mcp-*.whl 只匹配主包(其后紧跟 -),不会误匹配 redshift_mcp_sdk-*。
# --find-links /tmp/dist:host 声明 redshift-mcp-sdk 依赖,而该契约包只存在于本地 dist、未发布 PyPI;
# 把 dist 挂为额外查找源,sdk wheel 从本地解析,其余依赖(mcp/starlette/psycopg…)仍走 PyPI。
RUN uv venv /app/.venv \
    && VIRTUAL_ENV=/app/.venv uv pip install --find-links /tmp/dist /tmp/dist/redshift_mcp-*.whl \
    && rm -rf /tmp/dist /bin/uv
ENV PATH="/app/.venv/bin:$PATH" \
    REDSHIFT_MCP_CONFIG=/etc/redshift-mcp/config.yaml
# 日志目录;真实 config.yaml 等运行时以 volume 挂进 /etc/redshift-mcp,镜像内不含任何密钥
RUN mkdir -p /app/logs && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
# 服务无独立 health 端点且需 Bearer 鉴权,故用 TCP 连通性探活(镜像自带 python)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import socket;socket.create_connection(('127.0.0.1',8000),3).close()" || exit 1
ENTRYPOINT ["redshift-mcp"]
CMD ["--config", "/etc/redshift-mcp/config.yaml"]
