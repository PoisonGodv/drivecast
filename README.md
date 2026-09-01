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
- **逐集生成**：五段式脚本（S0 回顾上集自测 → S1 核心知识点 → S2 面试问法 → S3 答题框架 → S4 易错点+自测），支持独白/双人对话两种音色。
- **音频合成**：edge-tts 免费中文 Neural 音色，分 chunk 合成 + ffmpeg 响度归一/拼接/ID3，断点续合成。
- **开车友好播放**：Media Session 锁屏控制、±15s 大按钮、倍速、自动连播、进度记忆。
- **题库复习闭环**：每集自测题自动入库，三态掌握度（陌生/模糊/掌握）+ 简化间隔重复 + 重听联动 + 面试冲刺清单。

## 技术栈

Python 3.14 · FastAPI · SQLite(WAL) + SQLAlchemy 2.0 · openai-python（OpenAI 兼容协议，env 切厂商）· edge-tts · ffmpeg · 无构建前端（原生 ES Module + petite-vue，FastAPI 托管，同源无 CORS）

## 快速开始

> 当前进度：M0-M6 ✅（CLI / SQLite / Web / 复习闭环 / 成品音频打磨均已通过本地验收）。数据库升级请先运行 `.venv/bin/alembic upgrade head`（当前 head：`0004`）；维护或只读验收时可用 `WORKER_ENABLED=false python run.py` 禁止消费生成队列。细节见 [docs/05-roadmap.md](docs/05-roadmap.md)。

```bash
# 环境（一次性）
brew install ffmpeg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/smoke.py       # 依赖+LLM+edge-tts+ffmpeg 一把验
cp .env.example .env          # 填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

# 启动 Web（浏览器打开 http://127.0.0.1:8080）
python run.py
# 只读验收/维护：WORKER_ENABLED=false python run.py（不消费 script/tts/audio_upgrade）
# 手机/车机同一局域网访问:.env 加 WEB_HOST=0.0.0.0,然后 http://<本机IP>:8080
# 创建计划后 worker 后台逐集生成,边生成边听;锁屏可播放/±15s/切集(Media Session)

# CLI（与 Web 共用同一 DB 和 job 队列）
python -m drivecast plan "主题" --direction backend_arch --episodes 6
python -m drivecast episode <plan_id> 1     # 单集:脚本+音频
python -m drivecast generate <plan_id>      # 整计划批量
python -m drivecast list <plan_id>
python -m drivecast dbsync                  # 存量文件产物导入 SQLite
.venv/bin/alembic upgrade head              # DB 迁移（新环境一把建表）
.venv/bin/python -m unittest discover -s tests   # 跑全部测试
```

## 文档导航

| 文档 | 内容 |
|---|---|
| [docs/00-overview.md](docs/00-overview.md) | 系统全景图、核心数据流、跨文档决策裁定（矛盾点的权威结论） |
| [docs/01-product-content.md](docs/01-product-content.md) | 产品与内容体系：数据契约、五段脚本模板、题库结构、复习闭环 |
| [docs/02-architecture.md](docs/02-architecture.md) | 技术选型、模块划分、SQLite DDL、API 契约、LLM 接入层 |
| [docs/03-audio-pipeline.md](docs/03-audio-pipeline.md) | 音频管线：TTS 友好化、edge-tts 合成、ffmpeg 后处理、车载优化 |
| [docs/04-frontend.md](docs/04-frontend.md) | 前端：播放页（开车场景）、SSE 进度、题库复习页、无构建方案 |
| [docs/05-roadmap.md](docs/05-roadmap.md) | 分阶段实施路线图（WBS）、MVP 切点、各阶段验收标准 |
| [docs/06-deploy.md](docs/06-deploy.md) | 服务器部署指南：前置要求、步骤化部署、systemd、安全防护、备份与故障排查 |
| [docs/07-content-reliability.md](docs/07-content-reliability.md) | 内容可靠性契约：题库 grounding、事实审稿 pass、事实卡、勘误再生成 |
| [docs/08-qc-checklist.md](docs/08-qc-checklist.md) | 发布前真听质检 checklist（人审品控流程） |
