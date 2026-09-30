# 06 · drivecast 部署指南

> 本文档面向**在 Linux 服务器上执行部署的 agent / 运维人员**，按顺序执行即可完成部署。每步带验收标准，不通过不进下一步。
> 依赖索引：[README](../README.md) · 架构 [02](02-architecture.md) · 路线图 [05](05-roadmap.md)

## 1. 部署形态（先读懂再动手）

- **单 Python 进程**：`run.py` 启动 uvicorn，Web 服务与后台生成 worker **同进程**（`drivecast/web/main.py` lifespan 内嵌 `worker.run_forever()`），不需要单独的 worker 服务或消息队列。
- **存储**：SQLite（WAL 模式）+ 文件产物，全部落在项目目录 `data/` 下，无外部 DB 依赖。
- **前端**：无构建静态文件（`static/`），FastAPI 直接托管，无 Node/npm 环境需求。
- **外部网络依赖**（出站）：
  1. LLM API（OpenAI 兼容协议，地址由 `.env` 的 `LLM_BASE_URL` 决定）
  2. 微软 edge-tts 公共服务（`speech.platform.bing.com`，WSS 443）——音频合成走它
- **多用户认证**：Web 使用密码登录 + JWT Bearer，业务资源按用户隔离；没有公开注册 API，用户由本机 CLI 管理。对外仍必须启用 §6 的 TLS、代理信任与日志脱敏。

### 生成并发与长音频升级检查

- 保持单进程，不要增加 uvicorn worker 数，也不要同时启动 CLI worker 使用同一 `data/`。
- `WORKER_TEXT_CONCURRENCY=2`、`LLM_MAX_CONCURRENCY=6` 为默认；遇到模型端限流先降低 LLM 预算，不缩短长文请求超时。TTS 固定 `TTS_MAX_CONCURRENCY=1`。
- 检查现有 `.env` 的 `QUALITY_MAX_MINUTES`：旧模板是 22，40 分钟功能应配置为 45。新模板不会覆盖已有 `.env`。
- 重启后对已有误报音频调用带用户认证的 `POST /api/episodes/{id}/audio/recheck`，随后查询 job。此操作不重新生成音频，也不收取 TTS/LLM 费用；仍有本地音频分析 CPU 开销。质量范围变化后旧快照在重检前仍保留原结论。

## 2. 前置要求

| 项 | 要求 | 检查命令 |
|---|---|---|
| OS | 任意 Linux（systemd 优先） | `uname -a` |
| Python | **3.13 或 3.14**（开发验证 3.14；无 3.14 专属语法，3.13 可兜底；低于 3.13 不支持） | `python3 --version` |
| ffmpeg/ffprobe | 系统包即可，无需编译参数 | `ffmpeg -version && ffprobe -version` |
| 磁盘 | ≥2 GB（每集成品 MP3 约 9 MB + chunk 中间产物） | `df -h` |
| 出站网络 | 443 可达 LLM API 与 `speech.platform.bing.com` | 见 §3 步骤 6 冒烟 |
| 入站端口 | 默认 `8080`（可 `.env` 改 `WEB_PORT`） | `ss -ltnp \| grep 8080` |

## 3. 部署步骤

### 步骤 1 · 获取代码

```bash
# 方式 A：git（推荐）
git clone http://<git-host>/project/drivepodecast.git /opt/drivecast
cd /opt/drivecast

# 方式 B：从开发机 rsync（注意 .env 和 data/ 不在 git 里，见 §7 迁移存量数据）
# rsync -av --exclude .venv --exclude __pycache__ /path/to/drivecast/ user@server:/opt/drivecast/
```

> 下文以 `/opt/drivecast` 为部署目录，实际路径自行替换。

### 步骤 2 · 安装 ffmpeg（系统级）

```bash
# Debian/Ubuntu
sudo apt-get update && sudo apt-get install -y ffmpeg
# CentOS/RHEL（需要 epel/rpmfusion）
sudo dnf install -y epel-release && sudo dnf install -y ffmpeg
```

**验收**：`ffmpeg -version | head -1` 正常输出。

### 步骤 3 · 建 venv 装依赖

```bash
cd /opt/drivecast
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.lock   # 锁定版本，和开发环境一致
```

> 若个别包在服务器平台无 wheel 编译失败，改用 `pip install -r requirements.txt`（浮动小版本）；Python 换 3.13 重试是官方兜底方案（docs/02 §6）。

**验收**：`.venv/bin/python -c "import fastapi, uvicorn, sqlalchemy, edge_tts, openai, alembic; print('ok')"` 输出 `ok`。

### 步骤 4 · 配置 .env

```bash
if [ ! -e .env ]; then cp .env.example .env; fi  # 已有配置绝不能覆盖
chmod 600 .env
# 编辑 .env；已有 LLM/TTS 等配置保留，仅补缺失项：
#   LLM_BASE_URL=   # 如 https://api.deepseek.com/v1
#   LLM_API_KEY=    # 真实 key，勿提交到 git
#   LLM_MODEL=      # 如 deepseek-chat
#   AUTH_SECRET=    # 独立随机密钥（至少 32 字节随机源），不要复用 API key
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))'
# 将输出私密保存到 .env 的 AUTH_SECRET；已有 AUTH_SECRET 保持不变
# 推荐保持 WEB_HOST=127.0.0.1，由 §6 的 HTTPS 代理对外
```

其余项（音色/响度/时长）保持现有值或默认值。不要打印 `.env` 来验收；确认 LLM 配置和 `AUTH_SECRET` 均已填写。空签名密钥仅使用进程随机值，重启会令 token 失效，不适合部署；`WEB_HOST` 不是本机地址（127.0.0.1/localhost/::1）时，`run.py` 会直接拒绝启动。

### 步骤 5 · 建库（alembic 迁移）

```bash
.venv/bin/alembic upgrade head
```

**验收**：`sqlite3 data/drivecast.db "SELECT version_num FROM alembic_version"` 输出 `0006`。

升级已有库前必须停 Web/CLI worker 并备份 SQLite（含 WAL 状态，见 §7）和文件。迁移保留历史数据属主 `admin`（ID 1），初始密码为不可登录的锁定占位。新库同样预留该属主，不能用第一个普通用户接管历史数据。若遇到 schema 过旧、部分迁移或 admin/ID 1 冲突，**停止上线**，保留失败库和备份，排查冲突后重试；不要删除 user 表、强行 stamp head、手改归属或跳过校验。

迁移成功后再交互设置密码（不回显、需确认），不要把明文密码放到命令参数/历史里。没有设过密码时，启动日志和登录页都会提示执行这条命令：

```bash
.venv/bin/python -m drivecast reset-password admin
.venv/bin/python -m drivecast create-user alice  # 可选普通用户
```

无 TTY 自动部署使用秘密管理器注入 `DRIVECAST_USER_PASSWORD`，仅传给对应 CLI 子进程，完成后清除；不要写入脚本、`.env`、CI 日志或使用 `set -x`。CLI 虽支持 `--password`，但生产不推荐（进程列表和 shell 历史可泄露）。

### 步骤 6 · 冒烟（依赖 + LLM + edge-tts + ffmpeg 一把验）

```bash
.venv/bin/python scripts/smoke.py
```

**验收**：末行输出 `冒烟全绿 ✓`。任何一步失败：
- LLM 失败 → 检查 `.env` 三项与出站网络
- edge-tts 失败 → 检查到 `speech.platform.bing.com:443` 的出站 WSS（`curl -sI https://speech.platform.bing.com -m 5`）
- ffmpeg 失败 → 回步骤 2

### 步骤 7 · 跑测试（可选但建议）

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

**验收**：末行 `N passed`、无 failed。测试数据库/文件使用临时目录，业务 API 测试显式配置真实认证 fixture，不依赖 pytest autouse 桩。不要用 `unittest discover` 验收——它收集不到 `test_auth_bootstrap.py` 等 pytest 风格用例，会漏掉迁移与账号引导路径。

### 步骤 8 · 前台试跑

```bash
.venv/bin/python run.py
# 另开终端验收：
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/        # 200
curl -s -o /dev/null -w "%{http_code}\n" -H "Range: bytes=0-1023" \
  http://127.0.0.1:8080/api/episodes/1/audio                          # 401：未认证不可读音频
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/review/today  # 401
```

Ctrl+C 停掉，进入 §4 做常驻。

## 4. systemd 常驻

`/etc/systemd/system/drivecast.service`：

```ini
[Unit]
Description=DriveCast podcast generator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=deploy                    # 换成实际运行用户，对 /opt/drivecast 有写权限即可
WorkingDirectory=/opt/drivecast
ExecStart=/opt/drivecast/.venv/bin/python run.py
Restart=always
RestartSec=5
# run.py 最多等 10s(WEB_GRACEFUL_SHUTDOWN_SEC)让 SSE 等长连接断开,再把在途 job 放回队列;
# 留足余量,避免 systemd 在收尾前 SIGKILL
TimeoutStopSec=30
# 维护窗口暂停生成队列，可临时加：Environment=WORKER_ENABLED=false（不是只读）

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now drivecast
sudo systemctl status drivecast --no-pager     # Active: active (running)
```

**验收**：`curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/` → `200`。

## 5. 端到端验收（真实生成一集）

浏览器打开 §6 配置的 HTTPS 站点（或安全隧道的本机地址）：

1. 用刚设置的账号登录，再新建计划（选方向、6 集）→ 大纲确认 → 进入计划详情页。
2. 观察 SSE：约 2–5 分钟第一集变 ready（一次 LLM 脚本 + 若干 edge-tts chunk + ffmpeg 后处理）。
3. 播放第一集，声音正常、可拖进度条、可下载 MP3。
4. CLI 侧验证：`.venv/bin/python -m drivecast list <plan_id>` 状态一致。

> 不想花 LLM tokens 验收的话，可跳过本节，只做 §3 步骤 8 + §4 的健康检查。

## 6. 安全（登录不能替代传输与部署防护）

- 公网必须 HTTPS；推荐后端 `.env` 保持 `WEB_HOST=127.0.0.1`，只由 TLS 反向代理对外。也可仅用 SSH/Tailscale/WireGuard 隧道访问，不开放后端端口。
- JWT 默认有效期 7 天（`AUTH_TOKEN_EXPIRE_DAYS`）；**重置密码不会撤销已签发 token**，旧 token 到期前仍可能有效。客户端退出只清本地会话。怀疑泄露时需要轮换 `AUTH_SECRET` 并重启以使所有用户的旧 token 失效，再重新登录。
- 音频流/下载和 SSE 因浏览器接口限制会在白名单 URL 中携带 `?token=`。当前是同一登录 JWT，**不是短期、单资源授权 token**。Uvicorn 日志有应用内脱敏，但 nginx/CDN/WAF/负载均衡及监控采集仍需独立去除 query/token；不得缓存、转发或分享带 token 的链接。
- 登录限流按 Uvicorn 解析后的 `request.client.host` 工作，业务不自行信任任意 `X-Forwarded-For`。只有明确的代理源 IP 可以解析转发头；不得配置 `FORWARDED_ALLOW_IPS=*`。单机 nginx 可在 systemd 中显式设置 `Environment=FORWARDED_ALLOW_IPS=127.0.0.1`（如果代理实际用 IPv6/容器地址，改为明确地址）。后端不允许被客户端绕过代理直连。
- 若不使用代理，应关闭代理头解析（例如 `.venv/bin/python -m uvicorn drivecast.web.main:app --host 127.0.0.1 --port 8080 --no-proxy-headers`；lifespan 仍内嵌 worker）。不要启用多个 worker 进程。

nginx 片段（放在已经配置证书的 HTTPS server 中；另设 HTTP→HTTPS 重定向）：

```nginx
# http {} 范围定义；只记 $uri，不记含 query 的 $request/$request_uri
log_format drivecast_safe '$remote_addr $request_method $uri $status';

# HTTPS server {} 范围
access_log /var/log/nginx/drivecast.access.log drivecast_safe;
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr; # 单跳代理覆盖用户传入值
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;  # SSE 必须关缓冲
    proxy_read_timeout 300s;
}
```

多级代理必须逐跳建立信任并过滤客户端注入的头，不能直接照搬单跳示例。错误日志、追踪平台也可能记录完整请求 URL，需额外限制访问和脱敏；仅修改 access_log 不代表所有日志安全。可额外加防火墙白名单/Basic Auth，但不能替代应用认证或 TLS。

## 7. 数据与备份

- **全部状态在 `data/`**（SQLite + plans/scripts/tts/meta/audio 文件）+ `.env`。两者都在 `.gitignore` 里，git 部署不会带走。计划、脚本等 JSON 是运行必需的（打开计划、重试、重新合成都直接读），**只备份数据库和音频不能恢复**。
- **迁移存量数据**（把开发机内容搬到服务器）：停服务后 `rsync -av data/ .env user@server:/opt/drivecast/`，再 `alembic upgrade head` 补齐 schema。
- **日常备份**（服务不停也可执行）：
  ```bash
  .venv/bin/python scripts/backup.py --out /backup --keep 14
  ```
  产出单个 `drivecast-YYYYmmdd-HHMMSS.tgz`：数据库用 SQLite 在线快照（含 WAL 未合并页），加上 `data/` 下全部 JSON 与成品音频；默认跳过可重建的 `audio/chunks` 中间片（需要时加 `--with-chunks`），并只保留最近 14 份。`.env` 含密钥，**不进备份包**，单独存到秘密管理器。
  SQLite 开着 WAL，**不要**直接 `cp data/drivecast.db`（会漏 WAL 未合并页）。
- **定时**：`crontab -e` 加一行（每天 04:10）：
  ```cron
  10 4 * * * cd /opt/drivecast && .venv/bin/python scripts/backup.py --out /backup --keep 14 >> /var/log/drivecast-backup.log 2>&1
  ```
- **恢复**：停服务 → `tar xzf drivecast-XXXX.tgz -C /opt/drivecast`（覆盖 `data/`）→ 放回 `.env` → 启动（启动时自动迁移到 head）。恢复后抽查：`sqlite3 data/drivecast.db "pragma integrity_check"` 输出 `ok`。

## 8. 升级 / 回滚

```bash
set -e  # 任一步失败立即停止，不能继续启动
cd /opt/drivecast
sudo systemctl stop drivecast
# 先按 §7 备份数据库、文件和 .env，再继续
git fetch && git checkout <目标commit或tag>
.venv/bin/pip install -r requirements.lock
.venv/bin/alembic upgrade head
sudo systemctl start drivecast
```

回滚前停止所有进程，恢复与目标版本匹配的数据库/文件备份；不要直接让旧版无认证代码读取新多租户数据。0006 不提供自动安全降级，不能仅 checkout 旧代码后对外启动。升级迁移失败时不得执行 start；每一步成功后才继续。

## 9. 运维速查

| 场景 | 操作 |
|---|---|
| 暂停生成队列（不禁止 API 写入） | `WORKER_ENABLED=false` 启动，或 systemd unit 临时加该 Environment |
| 看日志 | `journalctl -u drivecast -f` |
| 某集生成失败 | Web 计划详情页该行"重试"，或 `POST /api/episodes/{id}/generate` |
| 整夜批量生成 | 建计划后放任即可，job 表持久化，进程重启自动续跑（running→pending 重入队） |
| edge-tts 偶发限流 | 已内置串行 + 指数退避，无需干预；持续 429/403 检查出站网络是否被墙 |
| 计划删除安全约束 | 删除依赖 `run.py` 单进程架构中止正在运行的任务；**严禁**在外置 CLI worker 消费时从 Web 执行删除 |
| 磁盘清理 | `data/audio/chunks/` 是中间产物，成品在 `data/audio/episodes/`，确认成品完好后可清 chunks |

## 10. 常见故障

| 症状 | 原因 → 处置 |
|---|---|
| 启动即退出，`Address already in use` | 8080 被占：改 `.env` 的 `WEB_PORT` 或释放端口 |
| 冒烟 LLM 步骤超时 | `LLM_TIMEOUT` 调大（长脚本生成实测 100-500s，建议 ≥600）；流式空闲看门狗 `LLM_IDLE_TIMEOUT`（默认 120s）判定连接挂死；或换厂商端点 |
| 冒烟 edge-tts 步骤失败 | 服务器到微软 TTS 出站不通（常见于国内机房）：配 HTTPS 代理或换出口 |
| SSE 无推送但页面正常 | 反向代理没关 buffering（§6 已含 `proxy_buffering off`） |
| `sqlite3.OperationalError: database is locked` | 多进程共写同一 DB；确认只有一个 drivecast 实例在跑 |
| 音频生成卡 running 不动 | 重启服务（断点续跑幂等，不会重复合成）；仍失败看 journalctl 对应 job 报错 |
