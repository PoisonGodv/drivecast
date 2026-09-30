# 00 · drivecast 系统总览与决策裁定

> 本文档是四份分文档的**汇总层**：系统全景、端到端数据流、关键决策汇总，以及跨文档矛盾点的**权威裁定**。
> 当分文档之间出现不一致时，以本文档「§4 决策裁定」为准；裁定已同步回改原文档。

---

## 1. 系统全景图

```
                          ┌─────────────────────────────────────────────┐
                          │                浏览器 (127.0.0.1)            │
                          │  ┌─────────┐ ┌─────────┐ ┌───────────────┐  │
                          │  │ 首页/创建│ │计划详情  │ │ 播放页(开车)   │  │
                          │  └────┬────┘ └────┬────┘ │ <audio>+Media │  │
                          │       │           │SSE   │  Session+连播  │  │
                          │       │           │      │ ┌─────────────┐│  │
                          │       │           │      │ │ 题库复习页   ││  │
                          └───────┼───────────┼──────┴─┴──────┬──────┴┼──┘
                                  │ REST/JSON │ text/event-stream │ Range 流
                                  ▼           ▼                 ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │                    FastAPI (单进程, 绑 127.0.0.1, 无鉴权)              │
   │  web/      api_plans · api_audio(Range) · api_review · plans/{id}/events│
   │  library/  只读聚合: 计划/集/进度/题库                                   │
   │  jobs/     asyncio worker + DB job 表(状态机, 重启续跑)                 │
   │     │ 编排 plan → script → tts 三步, 每步一个 job                       │
   │     ▼                                                                 │
   │  planner/        scriptwriter/         tts/                           │
   │  主题→N集大纲    大纲→五段脚本+题库     脚本→分chunk→edge-tts→ffmpeg    │
   │     │                │                      │                         │
   │     └────────────────┴──────► llm/ ◄────────┘                         │
   │                    LLMProvider(OpenAI兼容, schema强约束+self-heal)     │
   └───────┬───────────────────────────────────────────┬──────────────────┘
           │ SQLAlchemy 2.0 (WAL)                      │ subprocess
           ▼                                           ▼
   ┌────────────────┐   ┌──────────────────────────────────────────────┐
   │ SQLite         │   │ data/ 文件系统                                │
   │ drivecast.db   │   │  plans/ scripts/ tts/      ← 过程态中间产物    │
   │ topic plan     │   │  audio/chunks/{ep}/{seq}   ← 最小重试单元      │
   │ episode script │   │  audio/episodes/{ep}.mp3   ← 最终产物          │
   │ audio question │   │  meta/{ep}.json            ← 章节/时长        │
   │ job            │   └──────────────────────────────────────────────┘
   └────────────────┘            ▲                    ▲
                                 │ edge-tts (免费公共服务, 全局信号量限流)
                                 │ ffmpeg (loudnorm 归一/concat 拼接/ID3)
```

依赖方向：`web → library/jobs → planner/scriptwriter/tts → llm`，单向无环。内容生产**全部走 job 异步**，web 只触发与查询，播放永不被生成阻塞。

---

## 2. 核心数据流（从输入主题到听到音频）

```
用户输入主题「分布式锁」+ 方向 backend_arch + 集数(默认6)
  │
  ▼ ① POST /api/topics → POST /api/plans (入 job: kind=plan)
planner: LLM(schema=Plan) → 6 集递进大纲 → 落 plan/episode 表 + data/plans/{id}.json
  │     plan job 完成后停在 draft;用户在计划详情页删改大纲并点「确认大纲并开始生成」
  │     (PATCH /api/plans/{id}/outline → POST /api/plans/{id}/confirm) 后锁定 (status: draft→active)
  ▼ ② 逐集入 job (kind=script)   —— 可单集也可 generate_all 批量
scriptwriter: LLM(schema=ScriptOut)
  │   输入: 本集大纲 + 上集 3 个自测题(首集为空)
  │   输出: 五段脚本(S0回顾→S1知识点→S2问法→S3框架→S4易错+自测) + questions[]
  │   落库: script.content_md(权威, 供TTS+展示) + question 表(3题, mastery=unknown)
  │   过程态: data/scripts/{ep}.script.json
  ▼ ③ 入 job (kind=tts)
tts: TTS友好化(念法改写映射表, 确定性代码不靠LLM) → data/tts/{ep}.tts.json
  │   按 turn/语义分 chunk(200-400字) → 逐 chunk edge-tts(音色/rate/pitch)
  │   → data/audio/chunks/{ep}/{seq}.mp3   (失败只重试缺失 seq)
  │   → ffmpeg: 逐chunk loudnorm → 插静音 → concat → 整集二次归一 → ID3
  │   → data/audio/episodes/{ep}.mp3 + meta/{ep}.json(章节时间戳)
  │   落库: audio 表(file_path/duration_sec) + episode.status=ready
  ▼ ④ SSE 推送 episode_status(ready) → 前端该行亮▶
用户点播放: GET /api/episodes/{id}/audio (Range 流) → 锁屏 Media Session 控制
  │   每 10s POST progress; 播完自动连播下一集; listen_status→done
  ▼ ⑤ 复习闭环
到站翻题库: GET /api/review/today → 翻卡自测 → POST /api/questions/{id}/grade
  │   mastery 三态流转(unknown→fuzzy→mastered), next_review_at = +1/+2/+7 天
  ▼
面试前: 按 mastery!=mastered 过滤 → 重听 related_episodes → 考前 3 天冲刺清单
```

**断点续跑**：进程启动扫描 `job.status=running` 重置为 `pending` 重入队；episode 级幂等——已有 script/audio 产物则跳过对应阶段，只补缺失。

---

## 3. 四份文档关键决策汇总表

| 决策点 | 结论 | 出处 |
|---|---|---|
| 产品红线 | senior 深度、禁止科普水稿；每集固定五段结构 | 01 §1.2/§3.2 |
| 单集结构 | 五段 S0 回顾 / S1 知识点 / S2 问法 / S3 框架(总—分—钩) / S4 易错+3 自测 | 01 §3.2（权威） |
| 后端框架 | FastAPI + Uvicorn 单进程，绑 127.0.0.1 无鉴权 | 02 §1 |
| 存储 | SQLite(WAL) + SQLAlchemy 2.0 + alembic，可平迁 PG | 02 §1/§3 |
| 异步编排 | 进程内 asyncio worker + DB job 表，重启续跑；web 永不同步生成 | 02 §1/§2 |
| LLM 接入 | OpenAI 兼容协议，env 切厂商；schema 强约束 + self-heal 修复 | 02 §5 |
| 前端 | 无构建静态页 + petite-vue，FastAPI StaticFiles 托管，同源无 CORS | 04 §5（权威，02 已对齐） |
| 进度推送 | SSE 主通道 + 轮询兜底；不用 WebSocket | 04 §3（权威，02 已对齐） |
| 播放 | 原生 `<audio>` + Range + Media Session 锁屏控制 + 自动连播 | 04 §2 |
| TTS | edge-tts（免费中文 Neural），expert=YunxiNeural / host=XiaoxiaoNeural | 03 §3.1 |
| 音频后处理 | 必须装 ffmpeg：loudnorm(-16 LUFS) → 插静音 → concat → ID3 | 03 §4 |
| 语速换算 | rate=-5% 时 280-320 字/分，15 分钟 ≈ 4500 字，系数存配置校准 | 03 §6.1（权威，01 已对齐） |
| 复习机制 | 三态 mastery + 固定间隔(+1/+2/+7 天) + 重听联动 + 冲刺模式 | 01 §5（权威） |

---

## 4. 决策裁定（跨文档矛盾点的权威结论）

以下裁定均已回改原文档，此处为汇总备案。

| # | 矛盾点 | 冲突方 | 裁定 | 理由 |
|---|---|---|---|---|
| 1 | **前端框架** | 02: Vue3+Vite ↔ 04: 无构建+petite-vue | **无构建 + petite-vue**（04 为准，02 已改） | 5 个视图、交互不重，构建链是负担；复杂化后再迁 Vue3 |
| 2 | **进度推送** | 02: 轮询 jobs ↔ 04: SSE events | **SSE 主 + 轮询兜底** | 04 §3 论证完整；02 API 表已补 `/api/plans/{id}/events` |
| 3 | **direction 枚举** | 01: backend_arch/ai_llm/tech_news/source_dive ↔ 02 DDL 注释: backend/ai/news/source | **01 四值为准**（02 DDL 注释已改） | 01 是内容侧权威契约 |
| 4 | **episode 状态机** | 01: pending/generated/tts_done ↔ 02: pending/scripted/voiced/done ↔ 03: 七态 ↔ 04 SSE: ready | **生产态六值：`pending / scripted / tts_ready / synth_partial / ready / failed`**；消费态独立 `listen_status: unlistened/listening/done` | 融合 02/03/04：`ready`=可播放（=02 voiced=01 tts_done），`done` 只用于消费侧；02 `listened:int` 升级为 `listen_status` 三态 |
| 5 | **脚本存储** | 02: script.content_md 存 DB ↔ 03: scripts/*.json 存文件 | **正文权威存 DB，文件是过程态** | 02"避免双份"原则成立；03 的中间产物（scripts/tts/chunks/meta）保留作断点续做，可被 DB 重建 |
| 6 | **音频产物路径** | 02: data/audio/{ep}.mp3 ↔ 03: data/audio/episodes/{ep}.mp3 | **03 子目录结构**（02 目录树已改） | chunks 与成品需要分目录隔离 |
| 7 | **语速换算** | 01: 240-260 字/分 ↔ 03: 280-320 字/分 | **03 实测值为准**，15 分钟≈4500 字（01 已改） | 03 基于 rate=-5% 实测且带 ffprobe 回填校准机制 |
| 8 | **单集结构** | 01: 五段 ↔ 03: 四段 ↔ 02 prompt: 五小点 | **01 五段（S0-S4）为权威**；03 四段是音频分段视角的映射（开场≈S0+预告，正文≈S1-S3，快问快答≈S2/S3 高光，自测≈S4）；02 prompt 已对齐五段 | 01 是内容契约权威 |
| 9 | **mastery 模型** | 01/04: 三态 ↔ 02: INTEGER 0..5 + score | **三态 unknown/fuzzy/mastered**（02 DDL 与 grade API 已改） | 三态贴合口播自评与翻卡交互；0..5 粒度在车载/翻卡场景无意义 |
| 10 | **复习接口命名** | 02: /api/review/today + /api/questions/{id}/grade ↔ 04: /api/cards | **02 命名为准**，grade body `{mastery}`（04 已加注） | 与 question 表/01 题库模型一致 |
| 11 | **edge-tts 并发** | 02: 严格串行 ↔ 03: 全局 ≤3 | **全局信号量，默认并发 1，上限 3，配置可调** | 保守起步防限流，留出调优空间 |
| 12 | **difficulty 枚举** | 01: easy/medium/hard ↔ 02: 默认 'mid' | **easy/medium/hard**（02 已改） | 01 权威 |
| 13 | **默认集数** | 01 示例 6 ↔ 04 默认 8 | **默认 6，可调 4-12**（04 已改） | 对齐 01 递进示例 |
| 14 | **qtype 枚举** | 02: concept/scenario/design ↔ 01 自测规范: 概念/问法/应用 | **concept/interview/scenario**（02 已改） | 对齐 01 §5.2 三类自测题 |

### 遗留待联调确认（非矛盾，实现时验证）

- edge-tts 音色可用清单随微软侧变动，首次运行 `edge-tts --list-voices | grep zh-CN` 实测确认。
- Python 3.14 个别传递依赖 C 扩展 wheel 可能滞后，备 `import 全量`冒烟脚本 + pyenv 切 3.13 兜底（02 §6）。
- Safari/iOS 的 Media Session `setPositionState` 兼容性需真机验证（04 §2.2）。
