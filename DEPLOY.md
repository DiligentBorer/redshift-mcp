# 部署指南 —— redshift-mcp

目标拓扑：

- **OS**：RHEL / CentOS / Rocky / Alma / Amazon Linux 2023
- **进程管理**：`systemd`（开机自启、崩溃自动重启）
- **TLS 终结**：Nginx → 127.0.0.1:8000（redshift-mcp 只监听 loopback）
- **Python**：由 `uv` 管理（自动下载 CPython 3.13，不触碰系统 Python）
- **权限**：专属非特权 service user `redshift-mcp`

```
       Internet                           localhost
          │
   443 ─────────────► nginx ─────► 127.0.0.1:8000 ────► Redshift (5439)
   (TLS 终结)                  (HTTP, Bearer token)
```

## 0. 前提条件

- 一台干净的 RHEL 系服务器，能 `sudo`
- 出站网络：pypi / astral.sh（uv）/ Redshift 集群 5439 端口；如需从源码构建 wheel 还需 git
- 入站：**443/tcp** 对外开放（或仅对内 VPN）；
  **80/tcp** 仅用于 ACME challenge（Let's Encrypt）
- 一条 DNS A/AAAA 记录指向本机，例如 `redshift-mcp.example.com`
- Redshift 集群的 security group 已放行本机出口 IP

## 1. 装基础包

```bash
sudo dnf install -y \
    git curl tar gcc make openssl-devel \
    nginx firewalld policycoreutils-python-utils \
    certbot python3-certbot-nginx
sudo systemctl enable --now firewalld nginx
```

## 2. 装 uv（系统级）

uv 自带管理它自己的 Python —— 我们不动 OS Python：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
uv --version          # 期望: uv 0.11.x 或更新
```

放在 `/usr/local/bin/uv` 让所有用户（含 service user）都能直接用。

> 如果服务器已有 Python 3.10+，可跳过 uv，直接用系统 Python 的 venv + pip（见第 4 步方式二）。

## 3. 创建专属用户和目录

```bash
# 系统用户 —— 不可登录、专属 $HOME 用于存 uv 缓存
sudo useradd --system --create-home --home-dir /var/lib/redshift-mcp \
             --shell /sbin/nologin redshift-mcp

# 代码目录（service user 拥有 —— 它需要在这里创建 .venv）
sudo mkdir -p /opt/redshift-mcp
sudo chown redshift-mcp:redshift-mcp /opt/redshift-mcp

# 配置目录（含敏感凭证；权限收紧）
sudo mkdir -p /etc/redshift-mcp
sudo chown root:redshift-mcp /etc/redshift-mcp
sudo chmod 0750 /etc/redshift-mcp

# 日志目录（service user 可写）
sudo mkdir -p /var/log/redshift-mcp
sudo chown redshift-mcp:redshift-mcp /var/log/redshift-mcp
sudo chmod 0750 /var/log/redshift-mcp
```

## 4. 安装 redshift-mcp

### 方式一：直接安装预构建 wheel（推荐）

拿到发布 wheel 包（主程序 + 需要的插件），直接装进 venv：

```bash
# 1) 创建 venv（uv 自动下载 cpython-3.13）
sudo -u redshift-mcp -H bash -lc '
  cd /opt/redshift-mcp
  uv venv --python 3.13
'

# 2) 安装主程序 + 插件 wheel
sudo -u redshift-mcp -H bash -lc '
  cd /opt/redshift-mcp
  uv pip install /path/to/redshift_mcp-*.whl
  uv pip install /path/to/redshift_mcp_complex-*.whl   # 按需安装插件
'

# 3) 验证安装
sudo -u redshift-mcp -H bash -lc '
  cd /opt/redshift-mcp
  .venv/bin/redshift-mcp --version     # 打印版本号
  .venv/bin/redshift-mcp --list-plugins  # 列出已安装插件
'
```

装完后 `redshift-mcp` 命令直接在 `.venv/bin/` 下，**不需要 `uv run`**。

### 方式二：纯 pip 安装（无需 uv）

服务器已有 Python 3.10+ 时，不需要装 uv，用系统自带的 venv + pip 即可：

```bash
# 1) 创建 venv
sudo -u redshift-mcp -H python3 -m venv /opt/redshift-mcp/.venv

# 2) 安装主程序 + 插件 wheel
sudo -u redshift-mcp -H bash -lc '
  cd /opt/redshift-mcp
  .venv/bin/pip install /path/to/redshift_mcp-*.whl
  .venv/bin/pip install /path/to/redshift_mcp_complex-*.whl   # 按需安装插件
'

# 3) 验证安装
sudo -u redshift-mcp -H bash -lc '
  cd /opt/redshift-mcp
  .venv/bin/redshift-mcp --version     # 打印版本号
  .venv/bin/redshift-mcp --list-plugins  # 列出已安装插件
'
```

> 注意：`python3 -m venv` 要求系统已安装 Python 3.10+；RHEL 系可用 `sudo dnf install python3`。
> 如果系统 Python 版本过旧（< 3.10），请装 uv 后走方式一。

### 方式三：从源码构建 wheel（备选）

没有现成 wheel 时，从 git 仓库自构建：

```bash
# 在任意有 git 的机器上（不需要是生产服务器）
git clone <你的仓库地址> /tmp/redshift-mcp-src
cd /tmp/redshift-mcp-src
uv sync --all-packages
uv build --package redshift-mcp
uv build --package redshift-mcp-complex   # 按需构建插件
# 把 dist/ 下的 .whl 文件传到生产服务器，然后走方式一安装
```

> **提示**：如果插件自带外部配置（如 `complex` 的业务 SQL），见该插件 README —— 它自行从约定路径 / env var 加载，未配置则启动时报错跳过该插件（不影响其余工具）。

### 部署额外插件

业务工具以可安装包形式分发，装进与主程序**同一个 venv** 即被 entry_points 自动发现，
**不需要改 host 的任何配置**：

```bash
sudo -u redshift-mcp -H bash -lc '
  cd /opt/redshift-mcp
  uv pip install /path/to/my_plugin-*.whl
  # 纯 pip 等效: .venv/bin/pip install /path/to/my_plugin-*.whl
'
sudo systemctl restart redshift-mcp
```

启动后 `journalctl` 里会出现插件注册日志。临时禁用某个已装插件，
在 `config.yaml` 设 `plugins.disabled: ["<name>"]` 后重启即可。

> 若插件自带外部配置（如 `complex` 的业务 SQL），见该插件 README —— 它自行从约定路径 /
> env var 加载，未配置则启动时报错跳过该插件（不影响其余工具）。

### 声明式 SQL 工具（零代码）+ 配置拆分

简单 SQL 不必写 Python 插件，直接在 `config.yaml` 的 `sql_tools:` 段声明即可（详见 README「插件系统」），
启动 `journalctl` 会出现 `声明式 SQL 工具已注册: <name>`。声明式工具默认有只读安全闸门（`safe: true`）；
**LIMIT 可写可不写**——顶层缺 LIMIT 时自动追加 `LIMIT (max_rows+1)` 下推到 DB（journalctl 会记一条
`顶层无 LIMIT，已自动追加 LIMIT N`），显式写了则原样尊重。

条目多时可拆分：主 `config.yaml` 写 `include: ["conf.d/*.yaml"]`（glob 相对配置目录，即
`/etc/redshift-mcp/conf.d/`），把 `sql_tools` / `tables` 拆到片段；片段里 `sql_file: ../queries/x.sql`
可把大 SQL 外链到独立 `.sql`（相对片段目录）。这些目录都需与 config.yaml 同处 `/etc/redshift-mcp/`
且对 service user 可读（`systemd` 的 `ProtectSystem=strict` 下 `/etc` 默认只读、无需额外放行）。

## 5. 配置文件

```bash
sudo cp /opt/redshift-mcp/config.example.yaml /etc/redshift-mcp/config.yaml
sudo chown root:redshift-mcp /etc/redshift-mcp/config.yaml
sudo chmod 0640 /etc/redshift-mcp/config.yaml
sudo vi /etc/redshift-mcp/config.yaml
```

填入实际值：

```yaml
database:
  host: <redshift-cluster-endpoint>
  port: 5439
  dbname: <db>
  user: <readonly_user>
  password: <password>
  sslmode: require

server:
  host: 127.0.0.1               # 只听 loopback；外部流量全走 nginx
  port: 8000
  path: /redshift
  # 用如下命令生成: python3 -c "import secrets; print(secrets.token_urlsafe(48))"
  auth_token: <random-48-byte-base64>

query:
  statement_timeout_ms: 60000
  max_rows: 10000

plugins:
  enabled: true                 # false => 整体跳过插件加载
  disabled: []                  # 已安装但想临时禁用的插件名，例: ["complex"]

logging:
  level: INFO
  file: /var/log/redshift-mcp/redshift-mcp.log
  max_bytes: 10485760
  backup_count: 5
  as_json: false
```

## 6. systemd unit

创建 `/etc/systemd/system/redshift-mcp.service`：

```ini
[Unit]
Description=Redshift MCP Server (Streamable HTTP)
Documentation=file:///opt/redshift-mcp/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=redshift-mcp
Group=redshift-mcp
WorkingDirectory=/opt/redshift-mcp
Environment=REDSHIFT_MCP_CONFIG=/etc/redshift-mcp/config.yaml
ExecStart=/opt/redshift-mcp/.venv/bin/redshift-mcp --config /etc/redshift-mcp/config.yaml
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=15
StandardOutput=journal
StandardError=journal

# 沙箱加固
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/log/redshift-mcp /var/lib/redshift-mcp
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now redshift-mcp
sudo systemctl status redshift-mcp
sudo journalctl -u redshift-mcp -f --since "1 min ago"
```

首次启动应看到的日志行（中文）：

- `日志配置完成: level=INFO sql_audit_level=WARNING ... file=/var/log/redshift-mcp/redshift-mcp.log ...`
- `Redshift 连接池就绪 (host=... statement_timeout_ms=60000 ...)`
- `插件已加载: complex (redshift_mcp_complex:register)`
- `插件加载完成，共 1 个: complex` / `插件注册完成: ['complex']`
- `声明式 SQL 工具加载完成，共 N 个: ...` / `声明式 SQL 工具: [...]`（N=0 时为 `(无)`）
- `启动 redshift-mcp，监听 http://127.0.0.1:8000/redshift`
- `Uvicorn running on http://127.0.0.1:8000`

## 7. Nginx 反向代理

`/etc/nginx/conf.d/redshift-mcp.conf`：

```nginx
upstream redshift_mcp_upstream {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name redshift-mcp.example.com;
    location /.well-known/acme-challenge/ { root /var/lib/certbot; }
    location / { return 301 https://$server_name$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name redshift-mcp.example.com;

    # 第 9 步 certbot 会自动填充；下面是占位写法用于参考。
    ssl_certificate     /etc/letsencrypt/live/redshift-mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/redshift-mcp.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    server_tokens off;

    location = /redshift {
        proxy_pass http://redshift_mcp_upstream;
        include /etc/nginx/conf.d/redshift-mcp.proxy.inc;
    }
    location ^~ /redshift/ {
        proxy_pass http://redshift_mcp_upstream;
        include /etc/nginx/conf.d/redshift-mcp.proxy.inc;
    }

    location / { return 404; }
}
```

`/etc/nginx/conf.d/redshift-mcp.proxy.inc`：

```nginx
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto https;
proxy_set_header Connection        "";       # 走 upstream keepalive

# MCP Streamable HTTP 走 SSE —— buffering 必须关掉
proxy_buffering        off;
proxy_cache            off;
proxy_request_buffering off;
chunked_transfer_encoding on;

# 长超时（单次查询服务端最多 60s）
proxy_connect_timeout 10s;
proxy_send_timeout    300s;
proxy_read_timeout    300s;

proxy_pass_request_headers on;
```

校验并 reload：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 8. 防火墙 + SELinux

```bash
# firewalld
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --reload

# SELinux：允许 nginx 连接本机后端
sudo setsebool -P httpd_can_network_connect on

# 首次真实请求后，检查 AVC 拒绝记录并按需放行：
sudo ausearch -m AVC -ts recent | grep nginx || echo "no AVC denials"
```

## 9. 申请 TLS 证书（Let's Encrypt）

```bash
sudo certbot --nginx -d redshift-mcp.example.com \
             --agree-tos -m ops@example.com --redirect --non-interactive
sudo systemctl list-timers | grep certbot      # 自动续期已启用
```

certbot 会就地编辑 nginx 配置，插入证书路径并启用一个续期 timer。

## 10. 端到端验证

```bash
TOKEN="<config.yaml 里的 auth_token>"
DOMAIN="redshift-mcp.example.com"

# 1) 缺 token → 401
curl -i https://$DOMAIN/redshift

# 2) 合法 initialize → 200，SSE 响应体
curl -i -X POST https://$DOMAIN/redshift \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
          "params":{"protocolVersion":"2024-11-05","capabilities":{},
                    "clientInfo":{"name":"curl","version":"1"}}}'

# 3) 检查响应头里有 X-Request-ID；同一 rid 应在日志里出现
tail -f /var/log/redshift-mcp/redshift-mcp.log
```

客户端侧（Claude Desktop 通过 `mcp-remote`、Claude Code、MCP Inspector 等）：

- URL: `https://redshift-mcp.example.com/redshift`
- 头: `Authorization: Bearer <TOKEN>`

## 11. 升级流程

```bash
# 拿到新版本 wheel 后直接重装
sudo -u redshift-mcp -H bash -lc '
  cd /opt/redshift-mcp
  uv pip install --force-reinstall /path/to/redshift_mcp-新版本.whl
  # 纯 pip 等效: .venv/bin/pip install --force-reinstall /path/to/redshift_mcp-新版本.whl
  uv pip install --force-reinstall /path/to/redshift_mcp_complex-新版本.whl  # 按需
'
sudo systemctl restart redshift-mcp
sudo systemctl status redshift-mcp
```

## 12. 回滚

```bash
# 用上一版本的 wheel 重装
sudo -u redshift-mcp -H bash -lc '
  cd /opt/redshift-mcp
  uv pip install --force-reinstall /path/to/redshift_mcp-旧版本.whl
  uv pip install --force-reinstall /path/to/redshift_mcp_complex-旧版本.whl  # 按需
'
sudo systemctl restart redshift-mcp
```

## 13. 日常运维

| 操作 | 命令 |
| --- | --- |
| 看服务状态 | `sudo systemctl status redshift-mcp` |
| 实时看日志（journald） | `sudo journalctl -u redshift-mcp -f` |
| 实时看日志（滚动文件，含 `[rid=...]`） | `tail -f /var/log/redshift-mcp/redshift-mcp.log` |
| 按 request id 搜索 | `sudo grep "rid=ab12cd34" /var/log/redshift-mcp/*.log*` |
| 改配置后生效 | 编辑 `/etc/redshift-mcp/config.yaml` → `sudo systemctl restart redshift-mcp` |
| 改 unit 后生效 | `sudo systemctl daemon-reload && sudo systemctl restart redshift-mcp` |
| Nginx reload | `sudo nginx -t && sudo systemctl reload nginx` |
| 手动续期证书 | `sudo certbot renew` |

## 14. 监控建议（暂未接入）

- **liveness 探活**：`curl -fsS -H "Authorization: Bearer $TOKEN" -X POST https://.../redshift ... (initialize)`，检查 200
- **错误计数**：`tail` `redshift-mcp.log` 抓 `ERROR` 级别，转发到 CloudWatch / ELK
- **慢查询告警**：每次成功查询都会输出 `elapsed_ms=<N>`；按日志解析算 p95/p99

## 15. 路径速查

| 用途 | 路径 |
| --- | --- |
| 虚拟环境 | `/opt/redshift-mcp/.venv` |
| CLI 入口 | `/opt/redshift-mcp/.venv/bin/redshift-mcp` |
| Python 解释器 | `/var/lib/redshift-mcp/.local/share/uv/python/cpython-3.13.*/` |
| 配置 | `/etc/redshift-mcp/config.yaml`（0640 root:redshift-mcp） |
| 配置片段（可选，include 合并） | `/etc/redshift-mcp/conf.d/*.yaml` |
| 外链 SQL（可选，sql_file） | `/etc/redshift-mcp/queries/*.sql`（仓库内仅 `*.example.sql` 入 git，真实业务 SQL 在此目录自管） |
| 日志（滚动，5 × 10 MB） | `/var/log/redshift-mcp/redshift-mcp.log*` |
| systemd unit | `/etc/systemd/system/redshift-mcp.service` |
| Nginx 站点 | `/etc/nginx/conf.d/redshift-mcp.conf` + `redshift-mcp.proxy.inc` |
| TLS 证书 | `/etc/letsencrypt/live/redshift-mcp.example.com/` |

## 安全检查清单（上线前最后过一遍）

- [ ] 用的 Redshift 账号是**只读**，且权限仅限到所需的 schema/表
- [ ] `auth_token` **至少 32 字节随机**（用 `secrets.token_urlsafe(48)`）
- [ ] `/etc/redshift-mcp/config.yaml` 权限是 `0640 root:redshift-mcp` —— 用 `stat` 核对
- [ ] redshift-mcp 监听 **127.0.0.1** 而不是 0.0.0.0 —— 用 `ss -tlnp | grep 8000` 核对
- [ ] firewalld 只开了 443（以及 ACME 用的 80）；8000 不暴露
- [ ] HTTPS 正常，HTTP 重定向到 HTTPS
- [ ] Token 轮换流程已文档化（改 config + `systemctl restart`）
- [ ] 日志保留策略已确定（默认滚动 5 × 10 MB；按需调大）

## 不在本指南范围

- **容器化部署**（Docker / K8s） —— 也可行，但本服务足够小，systemd + nginx 更简单。若需要，写一份两阶段 Dockerfile（uv builder 阶段 + slim 运行时阶段）很直接。
- **高可用 / 多实例** —— 当前规模用不到；多实例需要按每实例独立连接池小心配额，确保集群整体连接数不超限。
- **WAF / IP 白名单** —— 若要限制调用方，在 Nginx 加 `allow`/`deny` 即可。
- **集中式日志** —— journald 与文件日志已就绪，转发到 ELK/CloudWatch 是 Logger / Fluent Bit 的事。
