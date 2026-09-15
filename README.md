# drivecast

**通勤面试学习播客生成器** —— 把每天开车的 40 分钟，变成面试准备的高效输入时间。

输入一个技术主题，AI 拆出学习计划、逐集生成**面试导向**的中文播客脚本，edge-tts 合成音频，本地浏览器播放，听完自动进题库复习，形成"路上听 → 到站自测 → 考前冲刺"的闭环。

## 这是什么 / 不是什么

| | |
|---|---|
| **是** | 结构化面试陪练音频：每集固定 知识点 → 高频问法 → 答题框架 → 易错点 → 3 个自测问题 |
| **不是** | 泛科普播客。红线：禁止"什么是 XX 它很好用"式水稿，默认听众已会用，讲的是原理、权衡、坑和面试怎么答 |

目标听众：资深后端工程师（有 Go 微服务 / 多租户 SaaS 实战经验），内容深度按 senior 档位对齐。

## 核心功能

- **四大内容方向**：① 后端/架构面试（Go/分布式/DB/缓存/MQ/系统设计）② AI/LLM 工程（RAG/Agent/微调/推理优化）③ 前沿技术资讯 ④ 源码/原理深读。每次使用仍可指定具体主题。
- **学习计划**：AI 把主题拆成 N 集递进大纲（原理 → 实现 → 权衡 → 实战 → 系统设计串联），可增删确认后锁定。
- **逐集生成**：五段式脚本（S0 回顾上集自测 → S1 核心知识点 → S2 面试问法 → S3 答题框架 → S4 易错点+自测），支持独白/双人对话两种音色；单集 10-40 分钟可选，长集自动把超长段拆成多个并行子 agent 分片写作再拼接。
- **音频合成**：TTS 双通道——edge-tts 免费中文 Neural（默认）/ 火山豆包 seed-tts-2.0（付费可选，一个 API Key 即可，音色更自然），分 chunk 合成 + ffmpeg 响度归一/拼接/ID3，断点续合成；片尾自动追加参考文献口播段（S5 章节，只念标题，链接留在文稿末尾），播完弹出参考文献面板。
- **开车友好播放**：Media Session 锁屏控制、±15s 大按钮、倍速、自动连播、进度记忆。
- **题库复习闭环**：每集自测题自动入库，三态掌握度（陌生/模糊/掌握）+ 简化间隔重复 + 重听联动 + 面试冲刺清单。

## 生成提速与旧音频重检

- 默认 2 路跨计划文本 job、6 路共享 LLM 请求预算；同计划逐集写稿保留回顾依赖。
- 脚本完成即允许合成，同时准备下一集文本。Edge/豆包 TTS 共用单路锁，不提高 TTS 并发。
- 质量范围默认 8–45 分钟，分别展示所选目标偏差与文本预测偏差。**旧 `.env` 若有 `QUALITY_MAX_MINUTES=22`，需改为 `45` 并重启**；修改模板不会覆盖现有配置。
- 已有音频无需重新生成：认证后 `POST /api/episodes/{数字集ID}/audio/recheck` 返回 job ID，通过 `GET /api/jobs/{job_id}` 查询。只更新质量快照，不调用 LLM/TTS、不重编码。GET 元数据中的 `quality.recheck_recommended` 提示规则是否过期。
- 不同时启动多个 Web worker 或额外 CLI 消费同一目录：请求预算和删除/生成锁限于单进程。

## 技术栈

Python 3.14 · FastAPI · SQLite(WAL) + SQLAlchemy 2.0 · openai-python（OpenAI 兼容协议，env 切厂商）· edge-tts（可切火山豆包 TTS）· ffmpeg · 无构建前端（原生 ES Module + petite-vue，FastAPI 托管，同源无 CORS）

## 快速开始

> Web 已启用多用户登录，计划、音频、题库和任务按用户隔离；没有公开注册 API。升级前停服务并备份 `data/` 与 `.env`，先迁移（当前 head：`0006`），再设置用户密码，最后启动。维护时 `WORKER_ENABLED=false` 仅禁止消费生成队列，并非只读或跳过认证。完整步骤见 [部署指南](docs/06-deploy.md)。

```bash
# 环境（一次性）
brew install ffmpeg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 仅新安装复制模板；已有 .env 必须保留，不要覆盖 LLM/TTS 等配置
if [ ! -e .env ]; then cp .env.example .env; fi
chmod 600 .env
# 编辑 .env 填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
# 已有配置仅补缺失的 AUTH_SECRET，保持现有密钥不变；生成随机值：
python -c 'import secrets; print(secrets.token_urlsafe(48))'
# 将输出私密保存为 .env 的 AUTH_SECRET=...，不要提交或贴到日志

.venv/bin/alembic upgrade head            # 失败必须停止，不能先启动/建普通用户
python -m drivecast reset-password admin # 交互输入并确认，解锁历史数据属主 admin
python -m drivecast create-user alice    # 可选：为其他使用者创建独立账号
python scripts/smoke.py                  # 真实调用外部依赖，可能消耗额度

# 启动 Web（浏览器打开 http://127.0.0.1:8080 后登录）
python run.py
# 暂停生成队列：WORKER_ENABLED=false python run.py（API 写操作仍开放）
# 手机/车机访问：按部署指南配置 HTTPS 代理或安全隧道，勿在明文公网传密码/token
# 创建计划后 worker 后台逐集生成,边生成边听;锁屏可播放/±15s/切集(Media Session)

# CLI（与 Web 共用同一 DB 和 job 队列）
python -m drivecast plan "主题" --direction backend_arch --episodes 6
python -m drivecast episode <plan_id> 1     # 单集:脚本+音频
python -m drivecast generate <plan_id>      # 整计划批量
python -m drivecast list <plan_id>
python -m drivecast dbsync                  # 存量文件产物导入 SQLite
.venv/bin/alembic upgrade head              # DB 迁移（新环境一把建表）
.venv/bin/python -m unittest discover -s tests   # 标准库测试 runner
.venv/bin/python -m pytest -q                    # 若已安装 pytest，可运行完整 Python 测试集
```

**认证边界**：改密码不会撤销旧 JWT；默认有效期 7 天，紧急全量失效需轮换 `AUTH_SECRET` 并重启。音频/SSE URL 携带的仍是登录 JWT，不是短期资源 token；需要 TLS，并对代理/CDN/WAF 的 URL 日志独立脱敏。只信任明确代理 IP 的转发头，禁止 `FORWARDED_ALLOW_IPS=*`。无 TTY 建号时由秘密管理器向 CLI 子进程注入 `DRIVECAST_USER_PASSWORD`，不要把密码写进命令参数、仓库或日志。

## 文档导航

| 文档 | 内容 |
|---|---|
| [docs/00-overview.md](docs/00-overview.md) | 系统全景图、核心数据流、跨文档决策裁定（矛盾点的权威结论） |
| [docs/01-product-content.md](docs/01-product-content.md) | 产品与内容体系：数据契约、五段脚本模板、题库结构、复习闭环 |
| [docs/02-architecture.md](docs/02-architecture.md) | 技术选型、模块划分、SQLite DDL、API 契约、LLM 接入层 |
| [docs/03-audio-pipeline.md](docs/03-audio-pipeline.md) | 音频管线：TTS 友好化、edge-tts/豆包合成、ffmpeg 后处理、车载优化 |
| [docs/04-frontend.md](docs/04-frontend.md) | 前端：播放页（开车场景）、SSE 进度、题库复习页、无构建方案 |
| [docs/05-roadmap.md](docs/05-roadmap.md) | 分阶段实施路线图（WBS）、MVP 切点、各阶段验收标准 |
| [docs/06-deploy.md](docs/06-deploy.md) | 服务器部署指南：前置要求、步骤化部署、systemd、安全防护、备份与故障排查 |
| [docs/07-content-reliability.md](docs/07-content-reliability.md) | 内容可靠性契约：题库 grounding、事实审稿 pass、事实卡、勘误再生成 |
| [docs/08-qc-checklist.md](docs/08-qc-checklist.md) | 发布前真听质检 checklist（人审品控流程） |
