# 05 · drivecast 实施路线图（WBS）

> 总原则：**先 CLI 跑通最小内容闭环，再补 Web**。内容质量（脚本深度 + 听感）是这个产品的命根，Web 只是外壳——先把"主题→计划→单集脚本→edge-tts 音频"在命令行跑通并**真听一集**，再投入前端。
> 每个阶段都有可验证的验收标准，不达标不进下一阶段。工作量按单人资深工程师估算（人日）。

---

## 阶段总览

| 阶段 | 名称 | 预估 | 关键产出 | 验收门禁 |
|---|---|---|---|---|
| M0 | 环境地基 | 0.5 d | venv + 依赖 + ffmpeg + LLM 连通 | 冒烟脚本全绿 |
| **M1** | **CLI 最小闭环（MVP 第一刀）** | **2.5 d** | 一条命令产出 1 集可播 mp3 | **真听 15 分钟不翻车** |
| M2 | 内容完整化 | 2.5 d | 整计划批量 + 对话式 + SQLite + 断点续跑 | 6 集计划一夜跑完，中断可续 |
| M3 | Web 后端 | 2 d | FastAPI REST + Range 流 + SSE | curl/浏览器裸测 API 全通 |
| M4 | Web 前端 | 2.5 d | 5 页 + 播放页 MediaSession + 复习页 | 手机锁屏可控，边生成边听 |
| M5 | 复习闭环增强 | 1.5 d | 间隔重复 + 重听联动 + 冲刺模式 | 三态流转与到期推荐正确 |
| M6 | 打磨 | 1 d | 章节/封面/下载/系数校准 | 成品感，可导出到播客 App |
| | **合计** | **~12.5 d** | | |

---

## M0 环境地基（0.5 d）

| # | 任务 | 产出 |
|---|---|---|
| M0-1 | pyenv 锁 3.14.7，建 venv，`requirements.txt` 钉小版本：fastapi / uvicorn / sqlalchemy[asyncio] / aiosqlite / alembic / openai / edge-tts / pydub / pydantic-settings | requirements.txt + lock |
| M0-2 | `brew install ffmpeg`，验证 `ffmpeg -version` / `ffprobe` | ffmpeg 可用 |
| M0-3 | `edge-tts --list-voices \| grep zh-CN` 实测音色清单，确认 YunxiNeural/XiaoxiaoNeural 在列 | 音色配置初值 |
| M0-4 | `config.py`（pydantic-settings）：LLM_BASE_URL/API_KEY/MODEL/TIMEOUT、音色对、并发、响度、字数系数、路径 | .env.example |
| M0-5 | 冒烟脚本：`import` 全量依赖 + 一次最小 LLM 调用 + 一句 edge-tts 合成 + 一次 ffmpeg loudnorm | scripts/smoke.py |

**验收**：冒烟脚本一把全绿；任一依赖无 3.14 wheel 则触发 02 §6 兜底（pyenv 切 3.13，配置即兼容）。

---

## M1 CLI 最小闭环 —— MVP 第一刀（2.5 d）

> **切点说明**：此阶段**不上 SQLite、不上 Web、不做对话式**。中间产物全落 `data/` 文件（03 §1 结构），单音色独白，先验证"内容质量 × 听感"这两个最大风险。DB 和双音色在 M2 补，不影响 M1 产物格式。

| # | 任务 | 细节 | 预估 |
|---|---|---|---|
| M1-1 | LLM 接入层 | `llm/provider.py` OpenAI 兼容 + schema 结构化输出 + self-heal 重出 + 指数退避（02 §5） | 0.5 d |
| M1-2 | planner | 主题+方向 → 6 集递进大纲（schema=Plan），落 `data/plans/{id}.json`；prompt 带 senior 画像防科普（02 §5） | 0.5 d |
| M1-3 | scriptwriter | 单集大纲 → 五段脚本（S0-S4，首集无 S0）+ 3 自测题；按 4500 字目标下 prompt；落 `data/scripts/{ep}.script.json` | 0.5 d |
| M1-4 | TTS 友好化 + 合成 | 念法改写映射表（03 §2.2，确定性代码）→ 分 chunk（200-400 字）→ edge-tts 单音色（YunxiNeural, rate=-5%）逐 chunk 落盘 → ffmpeg loudnorm+concat → `data/audio/episodes/{ep}.mp3` | 0.75 d |
| M1-5 | CLI 入口 | `python -m drivecast plan "<主题>" --direction backend_arch` / `episode <plan_id> <seq>`；参数：方向/集数/目标分钟 | 0.25 d |

**验收标准（门禁）**：
1. 一条命令从「分布式锁与 Redlock 争议」产出第 1 集 mp3，时长 8-22 分钟。
2. **真听全程**：术语念法正确（抽查 SETNX/Redlock/fencing token/QPS 各 ≥1 处无乱码）；无 markdown/代码朗读噪音。
3. 脚本含完整五段，S2 有连环追问、S3 有"总—分—钩"框架、S4 有 3 个自测题——**内容达到 senior 深度，不是科普**（人耳判）。
4. 单 chunk 合成失败重试该 chunk 即可续跑，不整集重来。

---

## M2 内容完整化（2.5 d）

| # | 任务 | 细节 | 预估 |
|---|---|---|---|
| M2-1 | SQLite + alembic | 02 §3 七表 DDL 落 migration；planner/scriptwriter 产物双写 DB（文件仍为过程态） | 0.5 d |
| M2-2 | job 表 + asyncio worker | 状态机 pending/running/done/failed；启动扫描 running→pending 续跑；episode 级幂等 | 0.5 d |
| M2-3 | 批量生成编排 | `plan → N×(script → tts)` pipeline；整计划 generate_all；集间串行防限流 | 0.5 d |
| M2-4 | 对话式脚本 | host/expert 双角色 turn 结构 + 双音色（Xiaoxiao/Yunxi）+ 角色间静音；ai_llm/tech_news 方向默认对话 | 0.5 d |
| M2-5 | 题库落库 | scriptwriter 的 questions[] 落 question 表（kp_id/qtype/framework/difficulty，mastery=unknown）；recap 串联上集自测题进 S0 | 0.5 d |

**验收**：6 集计划 generate_all 一夜跑完；中途 `kill` 进程重启后从断点续跑不重复合成；题库每集 3 题入库；第 2 集 S0 正确回顾第 1 集 3 问；对话集双音色切换自然。

---

## M3 Web 后端（2 d）

| # | 任务 | 细节 | 预估 |
|---|---|---|---|
| M3-1 | FastAPI 装配 | app/路由/StaticFiles/SPA fallback，绑 127.0.0.1 | 0.25 d |
| M3-2 | 计划/集 API | 02 §4 全表：topics/plans/episodes/generate*/jobs | 0.5 d |
| M3-3 | 音频流 | `/api/episodes/{id}/audio` 支持 Range（FileResponse），进度上报 | 0.25 d |
| M3-4 | SSE | `/api/plans/{id}/events`，worker 里程碑推 episode_status/plan_status（04 §3.2 协议） | 0.5 d |
| M3-5 | 复习 API | `/api/review/today` + `/api/questions/{id}/grade`（三态 + 固定间隔 next_review_at） | 0.5 d |

**验收**：curl 走通 02 §4 全部示例；Range 请求返回 206；SSE 用 `curl -N` 看到逐集状态事件；grade 后 next_review_at 按 +1/+2/+7 正确计算。

---

## M4 Web 前端（2.5 d）

| # | 任务 | 细节 | 预估 |
|---|---|---|---|
| M4-1 | 壳 + hash 路由 + api.js | static/ 无构建，petite-vue，深色主题 CSS 变量 | 0.5 d |
| M4-2 | 首页 + 创建计划 | 主题输入/方向四选/集数/大纲可删改确认 | 0.5 d |
| M4-3 | 计划详情 + SSE | 每集状态徽章 + 总进度条 + ready 高亮 + 失败重试 | 0.5 d |
| M4-4 | 播放页 | 大按钮 ≥96px、进度条大热区、倍速、Media Session、自动连播、进度 10s 落盘（04 §2） | 0.75 d |
| M4-5 | 题库复习页 | 翻卡（CSS 3D）、三态标记、按计划/标签/掌握度筛选（04 §4） | 0.5 d |

**验收**：手机（同一局域网）打开播放页，锁屏可播放/暂停/±15s/切集；一集播完自动连播；创建计划后第一集 ready 即可听、后续集后台继续生成（边生成边听）；题库翻卡标记后刷新状态保留。

---

## M5 复习闭环增强（1.5 d）

> 状态：✅ 已完成并通过本地验收。`0002` 补齐复习时间、知识点归一、次日重听队列与面试日；`0004` 修复 ORM 先建表时的 SQL 默认值/单例约束并回填历史复习时间。持久化队列默认消费今天到期任务，今天为空才预览明日；API 直接返回数值 `plan_id`，旧空 `kp_id` 仍可读。

| # | 任务 | 细节 | 预估 |
|---|---|---|---|
| M5-1 | 间隔重复完整化 | review_count/last_reviewed 补齐；到期排序；答错回退 unknown | 0.5 d |
| M5-2 | 重听联动 | 知识点 fuzzy/unknown ≥2 → 推荐重听最短相关集 + 入次日队列 | 0.5 d |
| M5-3 | 面试冲刺模式 | 设面试日 → mastery!=mastered 按 direction 聚合生成考前 3 天重听清单 | 0.5 d |

**验收**：01 §5.3 三态流转与间隔全对；冲刺清单按题数排序正确。

---

## M6 打磨（1 d）

> 状态：✅ 已完成并通过本地验收。`0003` 持久化音频元数据/质检状态，`0004` 为存量成品排本地 `audio_upgrade`；当前音频管线版本 7 复用已有脚本、`tts.json` 与 chunk，只做 ffmpeg 后处理，不补调 LLM/edge-tts。文件态 meta 缺失或损坏时回退 DB 快照，旧 MP3、旧 meta 与旧 `tts.json` 保持可读。

| # | 任务 | 细节 |
|---|---|---|
| M6-1 | 章节标记 | meta/{ep}.json 章节时间戳 → 播放页进度条分段跳转；（可选）ffmetadata 写 CHAPTER |
| M6-2 | ID3 + 封面 | 标题/artist/album/track + 统一封面内嵌；mp3 下载出口 |
| M6-3 | 语速系数校准 | ffprobe 真实时长回填，校正准备字数系数存配置 |
| M6-4 | 质量校验自动跑 | 03 §6.2：时长区间/削波/chunk 数一致 |

**验收**：真实 MP3 经 `ffprobe` 验证 ID3、内嵌封面、5 个章节与时长；峰值低于 -0.5 dB；8 个存量成品的校准时长误差均 <10%。FastAPI 在 `WORKER_ENABLED=false` 下完成元数据、Range 206、下载与静态资源冒烟。

---

## 风险与取舍备注

- **最大风险在 M1**：内容水不水、术语念得对不对，M1 验收必须**真听**，不走过场。不达标就回去调 prompt 和念法映射表，别带病进 M2。
- **M2 才上 DB** 是有意的：M1 文件产物格式与 DB 字段同源（schema 复用），迁移成本近零；避免在内容质量未验证时过早建表。
- **Web（M3/M4）与内容（M2）可并行**若多人，但单人建议串行：Web 依赖 M2 的 job/SSE 数据源语义稳定。
- **显式不做**（本期）：注册登录、多用户、Service Worker 离线、完整 SM-2/Anki、移动端原生 App、按 plan 清理磁盘接口（V2）。
