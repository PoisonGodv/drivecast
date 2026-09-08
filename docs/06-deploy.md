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
- **无鉴权**：应用本身没有登录/认证（本地单用户设计）。对外暴露**必须**走 §6 的防护措施。

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
cp .env.example .env
chmod 600 .env
# 编辑 .env，必填三项：
#   LLM_BASE_URL=   # 如 https://api.deepseek.com/v1
#   LLM_API_KEY=    # 真实 key，勿提交到 git
#   LLM_MODEL=      # 如 deepseek-chat
# 服务器对外提供访问时必须加：
#   WEB_HOST=0.0.0.0
```

其余项（音色/响度/时长）保持默认即可。**验收**：`grep -c '^LLM_' .env` ≥ 3。

### 步骤 5 · 建库（alembic 迁移）

```bash
.venv/bin/alembic upgrade head
```

**验收**：`sqlite3 data/drivecast.db "SELECT version_num FROM alembic_version"` 输出 `0004`。

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
.venv/bin/python -m unittest discover -s tests
```

**验收**：`OK`（当前 19 个用例）。

### 步骤 8 · 前台试跑

```bash
.venv/bin/python run.py
# 另开终端验收：
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/        # 200
curl -s -o /dev/null -w "%{http_code}\n" -H "Range: bytes=0-1023" \
  http://127.0.0.1:8080/api/episodes/1/audio                          # 404 也算过：新库无集，只验路由活着
curl -s http://127.0.0.1:8080/api/review/today                        # 返回 JSON（空数组也算过）
```

Ctrl+C 停掉，进入步骤 9 做常驻。

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
# 维护窗口只读运行时，可临时加：Environment=WORKER_ENABLED=false

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

浏览器打开 `http://<服务器IP>:8080/`（先确认 §6 防护就位）：

1. 新建计划（选方向、6 集）→ 大纲确认 → 进入计划详情页。
2. 观察 SSE：约 2–5 分钟第一集变 ready（一次 LLM 脚本 + 若干 edge-tts chunk + ffmpeg 后处理）。
3. 播放第一集，声音正常、可拖进度条、可下载 MP3。
4. CLI 侧验证：`.venv/bin/python -m drivecast list <plan_id>` 状态一致。

> 不想花 LLM tokens 验收的话，可跳过本节，只做 §3 步骤 8 + §4 的健康检查。

## 6. 安全（必读：应用无鉴权）

`WEB_HOST=0.0.0.0` 后**同一网络任何人都能访问全部功能**（创建计划消耗你的 LLM 配额、听全部音频）。三选一：

1. **防火墙白名单**（最简单）：只放行你的常用 IP / 内网段。
   ```bash
   sudo ufw allow from <你的IP> to any port 8080 proto tcp
   sudo ufw deny 8080
   ```
2. **反向代理 + Basic Auth**（nginx 片段）：
   ```nginx
   location / {
       auth_basic "drivecast";
       auth_basic_user_file /etc/nginx/.htpasswd;   # htpasswd -c 生成
       proxy_pass http://127.0.0.1:8080;
       proxy_set_header Host $host;
       proxy_buffering off;        # SSE 必须关缓冲
   }
   ```
   此时 `.env` 保持 `WEB_HOST=127.0.0.1`，由 nginx 对外。
3. **仅隧道访问**：保持 `127.0.0.1` 绑定，用 `ssh -L 8080:127.0.0.1:8080 user@server` 或 Tailscale/WireGuard 进。

> 注意：手机锁屏 Media Session、SSE 长连接在反向代理后都要 `proxy_buffering off`，方案 2 已含。

## 7. 数据与备份

- **全部状态在 `data/`**（SQLite + plans/scripts/tts/audio 文件）+ `.env`。两者都在 `.gitignore` 里，git 部署不会带走。
- **迁移存量数据**（把开发机内容搬到服务器）：停服务后 `rsync -av data/ .env user@server:/opt/drivecast/`，再 `alembic upgrade head` 补齐 schema。
- **日常备份**：
  ```bash
  sqlite3 data/drivecast.db ".backup '/backup/drivecast-$(date +%F).db'"
  tar czf /backup/drivecast-audio-$(date +%F).tgz data/audio/episodes/
  ```
  SQLite 开着 WAL，**不要**直接 `cp data/drivecast.db`（会漏 WAL 未合并页）。

## 8. 升级 / 回滚

```bash
cd /opt/drivecast
sudo systemctl stop drivecast
git fetch && git checkout <目标commit或tag>
.venv/bin/pip install -r requirements.lock
.venv/bin/alembic upgrade head
sudo systemctl start drivecast
```

回滚：`git checkout <旧commit>` 后同样步骤；alembic 迁移未写 downgrade，回滚 schema 不支持，靠备份 `.db` 恢复。

## 9. 运维速查

| 场景 | 操作 |
|---|---|
| 只读维护（不消费生成队列） | `WORKER_ENABLED=false` 启动，或 systemd unit 临时加该 Environment |
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
| SSE 无推送但页面正常 | 反向代理没关 buffering（§6 方案 2 已含 `proxy_buffering off`） |
| `sqlite3.OperationalError: database is locked` | 多进程共写同一 DB；确认只有一个 drivecast 实例在跑 |
| 音频生成卡 running 不动 | 重启服务（断点续跑幂等，不会重复合成）；仍失败看 journalctl 对应 job 报错 |
