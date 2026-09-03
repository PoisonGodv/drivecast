# DriveCast 系统架构设计

> 定位：个人通勤面试学习工具。用户给主题 → AI 出学习计划 → 逐集生成脚本 → edge-tts 合成中文音频 → 本地流式播放 + 题库复习。
> 读者是资深后端工程师，内容深度与工程质量对齐，不做玩具。

---

## 1. 技术选型

| 维度 | 选型 | 理由 | 否决项 |
|---|---|---|---|
| 后端框架 | **FastAPI** | 原生 async（edge-tts、LLM 调用都是 IO 密集）；Pydantic 契约即文档，API 模型与 LLM 结构化输出复用同一套 schema；自带 OpenAPI/Swagger 便于个人调试；Uvicorn 单进程够用 | Flask：同步模型，TTS/LLM 长 IO 会阻塞，需额外 gevent/线程池，契约手写 |
| 存储 | **SQLite (WAL)** | 单用户单进程，并发=1~2；零运维、单文件可备份；WAL 模式读写不互斥，够支撑「边生成边播放」。真到瓶颈再迁 PG，SQLAlchemy 抽象层已隔离 | 直接 PG：个人工具过度设计 |
| ORM | **SQLAlchemy 2.0 (Core+ORM, asyncio)** | 统一 DDL 与查询，迁移用 alembic；与 SQLite/PG 切换零业务改动 | 裸 sqlite3：SQL 散落难维护 |
| 前端 | **无构建静态页 + petite-vue（约 6KB）** | 个人工具优先轻量：5 个视图（首页/创建/计划详情/播放器/题库），交互不重，上全家桶是过度工程；FastAPI `StaticFiles` 直接托管 `static/`，**零 node_modules、改动即刷新、同源无 CORS**；`<audio>` 原生支持 Range 流式播放。交互复杂度上升后再平滑迁 Vue3+Vite | Vue3+Vite/React：MVP 阶段构建链与依赖是负担；服务端模板：播放进度、SSE、题库翻面等交互体验差 |
| 任务队列 | **asyncio + 进程内 asyncio.Queue + 持久化 job 表** | 音频/脚本生成是分钟级长任务，但单用户。用 DB 表做 job 状态机（pending/running/done/failed），进程内 worker 消费，**重启后从 job 表恢复**，天然支持断点续跑 | Celery/Redis：个人工具引入 broker 是负担 |
| LLM SDK | **openai-python (兼容协议)** | 用户已有 LLM API，走 OpenAI 兼容 base_url 可覆盖绝大多数厂商；`response_format`/工具调用做结构化输出 | 各厂商原生 SDK：锁定单一 provider |
| 音频 | **edge-tts** | 免费、中文 `zh-CN-XiaoxiaoNeural` 等音色自然、无需密钥；同步流式写文件 | 商用 TTS：成本与密钥管理无必要 |

**Python 3.14 说明**：FastAPI/SQLAlchemy/openai 主干已支持。风险在个别传递依赖的 C 扩展 wheel 滞后，对策见 §6。建议 pyenv 锁定 3.14.7，requirements 用 `~=` 钉小版本并提交 `requirements.lock`。

---

## 2. 模块划分

### 目录结构

```
drivecast/
├── docs/
│   └── 02-architecture.md
├── pyproject.toml / requirements.txt
├── run.py                        # 入口: uvicorn 启动 + 内嵌 worker 开关
├── drivecast/
│   ├── config.py                 # pydantic-settings: LLM base_url/key、音色、并发、路径
│   ├── db.py                     # engine/session 工厂、WAL pragma、alembic env
│   ├── models.py                 # SQLAlchemy 表(§3)
│   ├── schemas.py                # Pydantic API 契约(§4) + LLM 输出契约
│   ├── llm/
│   │   ├── provider.py           # LLMProvider 抽象 + OpenAI 实现(§5)
│   │   ├── prompts.py            # plan/script/question 的 prompt 模板(§5)
│   │   └── retry.py              # 退避重试/降级/结构化输出校验修复
│   ├── planner/
│   │   └── service.py            # 主题→学习计划(拆 episode 大纲), 落 plan/episode 表
│   ├── scriptwriter/
│   │   └── service.py            # episode 大纲→面试导向脚本(知识点+问法+答题框架)
│   ├── tts/
│   │   ├── edge.py               # edge-tts 封装: 脚本→mp3, 流式写, 限流/重试
│   │   └── service.py            # 分段合成→合并, 产物落 audio 表
│   ├── library/
│   │   └── service.py            # 内容库查询: 计划/集/进度/题库聚合
│   ├── jobs/
│   │   ├── worker.py             # asyncio worker 消费 job 表, 状态机推进
│   │   └── pipeline.py           # 编排: plan→script→tts 各阶段入 job
│   └── web/
│       ├── main.py               # FastAPI app 装配、路由、StaticFiles、SPA fallback
│       ├── api_plans.py          # 计划/集/生成触发
│       ├── api_audio.py          # 音频流式播放(Range)、进度上报
│       └── api_review.py         # 题库复习
├── alembic/versions/
├── static/                       # 无构建前端(原生 ES Module + petite-vue), 由 FastAPI StaticFiles 托管
│   ├── index.html
│   ├── css/{base,components}.css
│   ├── js/{main,api,player,progress,review,store}.js
│   └── assets/cover.png
└── data/
    ├── drivecast.db              # SQLite
    ├── plans/{plan_id}.json      # planner 中间产物(过程态)
    ├── scripts/{ep_id}.script.json   # scriptwriter 中间产物(按 turn 分说话人)
    ├── tts/{ep_id}.tts.json      # TTS 友好化分段文本(voice/rate/pitch)
    ├── audio/chunks/{ep_id}/{seq}.mp3  # 逐段音频(最小合成/重试单元)
    ├── audio/episodes/{ep_id}.mp3      # 最终整集(归一+拼接+ID3)
    └── meta/{ep_id}.json         # 章节标记/时长/字数统计
```

> 脚本正文权威存 DB（`script.content_md`，供 TTS 朗读与前端展示，避免双份）；`data/` 下的 plans/scripts/tts/meta 文件是**过程态中间产物**，专供断点续做与排查，可被 DB 重建。音频最终产物统一 `data/audio/episodes/{episode_id}.mp3`（03 的子目录结构），DB `audio.file_path` 记录之。

### 模块边界

- **planner**：输入主题+方向，产出一份 plan（含 N 个 episode 的有序大纲、每集目标知识点）。只写 `plan`/`episode`，不碰脚本与音频。
- **scriptwriter**：输入单个 episode 大纲，产出**面试导向脚本**（结构化：开场钩子 → 知识点精讲 → 常见面试问法 → 答题框架/STAR → 本集小结），并抽取**question**（题库）。只写 `script`/`question`。
- **tts**：输入 script 文本，产出 mp3 + 时长。只写 `audio`。对 edge-tts 用**全局信号量限流**（默认并发 1，上限 3，配置 `TTS_MAX_CONCURRENCY` 可调，见 03 §3.3）+ 退避。
- **library**：只读聚合层，给 web 提供「计划详情/集列表/进度/题库」视图，不产生内容。
- **jobs**：异步编排。`plan→script→tts` 三步每步一个 job，job 表持久化，worker 串行消费（防 edge-tts 限流）。**内容模块只被 jobs 调用，web 永不直接同步生成**——web 只入 job 并返回 job_id 供轮询。
- **web**：REST + 静态托管 + SPA fallback。无鉴权（本机单用户），仅绑 `127.00.0.1`。

依赖方向：`web → library/jobs → planner/scriptwriter/tts → llm`，单向无环。

---

## 3. 数据模型 (SQLite DDL)

```sql
-- 主题/学习请求
CREATE TABLE topic (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL,                -- 用户输入的具体主题, 如 "Go GC 调优"
  direction     TEXT NOT NULL,                -- backend_arch/ai_llm/tech_news/source_dive 四方向(以 01 文档为准)
  depth         TEXT NOT NULL DEFAULT 'senior',
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 学习计划
CREATE TABLE plan (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id      INTEGER NOT NULL REFERENCES topic(id),
  title         TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'draft', -- draft/active/done/archived
  episode_count INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 集(播客单集)
CREATE TABLE episode (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id       INTEGER NOT NULL REFERENCES plan(id),
  seq           INTEGER NOT NULL,             -- 计划内顺序
  title         TEXT NOT NULL,
  outline       TEXT NOT NULL,                -- planner 产出的大纲(JSON 字符串)
  objectives    TEXT NOT NULL DEFAULT '',     -- 本集目标知识点(JSON array)
  status        TEXT NOT NULL DEFAULT 'pending', -- 生产状态机: pending/scripted/tts_ready/synth_partial/ready/failed
  position_sec  INTEGER NOT NULL DEFAULT 0,   -- 播放进度(续播)
  listen_status TEXT NOT NULL DEFAULT 'unlistened', -- 消费状态: unlistened/listening/done
  UNIQUE(plan_id, seq)
);

-- 脚本
CREATE TABLE script (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id    INTEGER NOT NULL UNIQUE REFERENCES episode(id),
  content_md    TEXT NOT NULL,                -- 结构化脚本文本(供 TTS 朗读 + 页面展示)
  knowledge     TEXT NOT NULL DEFAULT '',     -- 知识点摘要(JSON)
  token_usage   INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 音频产物
CREATE TABLE audio (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id    INTEGER NOT NULL UNIQUE REFERENCES episode(id),
  file_path     TEXT NOT NULL,                -- data/audio/{episode_id}.mp3
  voice         TEXT NOT NULL,
  duration_sec  INTEGER NOT NULL DEFAULT 0,
  size_bytes    INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'done', -- rendering/done/failed
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 题库(从脚本抽取的面试题)
CREATE TABLE question (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id    INTEGER NOT NULL REFERENCES episode(id),
  kp_id         TEXT NOT NULL DEFAULT '',     -- 知识点 id(题库的索引维度,见 01 §4)
  qtype         TEXT NOT NULL DEFAULT 'concept', -- concept/interview/scenario
  question      TEXT NOT NULL,
  framework     TEXT NOT NULL,                -- 答题框架/要点(JSON array)
  difficulty    TEXT NOT NULL DEFAULT 'medium', -- easy/medium/hard(01 权威)
  mastery       TEXT NOT NULL DEFAULT 'unknown',  -- unknown/fuzzy/mastered 三态(01/04 权威)
  review_count  INTEGER NOT NULL DEFAULT 0,
  next_review_at TEXT,                        -- 简易间隔重复: unknown+1d/fuzzy+2d/mastered+7d
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 异步任务(长任务编排 + 断点续跑核心)
CREATE TABLE job (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,                -- plan/script/tts
  ref_id        INTEGER NOT NULL,             -- 关联 plan_id 或 episode_id
  status        TEXT NOT NULL DEFAULT 'pending', -- pending/running/done/failed
  attempt       INTEGER NOT NULL DEFAULT 0,
  max_attempt   INTEGER NOT NULL DEFAULT 3,
  error         TEXT NOT NULL DEFAULT '',
  payload       TEXT NOT NULL DEFAULT '{}',   -- 阶段参数(JSON)
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_job_status ON job(status, id);
CREATE INDEX idx_episode_plan ON episode(plan_id, seq);
CREATE INDEX idx_question_review ON question(next_review_at);
```

设计要点：`episode.status` 与 `job` 双写，job 是「过程态」、episode 是「结果态」；`script.content_md` 同时供 TTS 朗读与前端展示，避免双份。`job.payload` 携带阶段参数，worker 重启后据此续跑。

---

## 4. API 契约

统一前缀 `/api`，JSON，错误体 `{"code":..., "message":...}`。本机单用户，无鉴权。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/topics` | 创建主题 |
| POST | `/api/plans` | 由主题生成学习计划（入 job，异步） |
| GET | `/api/plans` | 计划列表 |
| GET | `/api/plans/{id}` | 计划详情（含 episodes + 各集 status/进度） |
| POST | `/api/plans/{id}/cancel` | 取消计划：清 pending job、未完成集标 cancelled（终态）；已就绪集保留可播放；幂等 |
| POST | `/api/plans/{id}/activate` | 恢复已取消计划：cancelled 集回 pending，重新入队未完成部分 |
| POST | `/api/episodes/{id}/generate` | 触发该集 script+tts（入 job；计划已取消返回 409） |
| POST | `/api/plans/{id}/generate_all` | 整计划批量生成 |
| GET | `/api/jobs/{id}` | 任务状态查询（兜底;主推送走 SSE） |
| GET | `/api/plans/{id}/events` | **SSE 生成进度推送**（主通道,协议见 04 §3） |
| GET | `/api/episodes/{id}/script` | 取脚本文本（页面跟读） |
| GET | `/api/episodes/{id}/audio` | **流式播放（支持 Range）** |
| POST | `/api/episodes/{id}/progress` | 上报播放进度/听完 |
| GET | `/api/review/today` | 今日待复习题（next_review_at 到期） |
| POST | `/api/questions/{id}/grade` | 复习自评（更新 mastery + 下次复习时间） |

**示例：创建并生成计划**

```http
POST /api/plans
{ "topic_id": 3, "episode_count": 6 }

201
{ "plan_id": 12, "job_id": 88, "status": "pending" }
```

**轮询任务**（SSE 断线时的兜底手段）

```http
GET /api/jobs/88
200
{ "id": 88, "kind": "plan", "status": "running", "attempt": 1, "error": "" }
```

**计划详情（前端播放器/集列表数据源）**

```http
GET /api/plans/12
200
{
  "id": 12, "title": "Go GC 调优速成", "status": "active",
  "episodes": [
    {"id": 101, "seq": 1, "title": "三色标记与写屏障", "status": "ready",
     "has_audio": true, "duration_sec": 612, "position_sec": 300, "listen_status": "listening"},
    {"id": 102, "seq": 2, "title": "GOMEMLIMIT 与 GC pacer", "status": "scripted",
     "has_audio": false}
  ]
}
```

**流式播放**（前端 `<audio src>` 直接指向，浏览器自动发 Range）

```http
GET /api/episodes/101/audio   Range: bytes=0-
206  Content-Type: audio/mpeg  Content-Range: bytes 0-1048575/5242880
```

**复习自评**（三态,与 01 §5.3 / 04 §4.1 对齐）

```http
POST /api/questions/501/grade
{ "mastery": "fuzzy" }        # unknown | fuzzy | mastered
200
{ "id": 501, "mastery": "fuzzy", "next_review_at": "2026-08-23T08:00:00" }
```

---

## 5. LLM 接入层设计

### Provider 抽象

```python
class LLMProvider(Protocol):
    async def complete(self, *, system: str, user: str,
                       schema: type[BaseModel] | None = None,
                       temperature: float = 0.7) -> LLMResult: ...

class OpenAICompatProvider:   # 走 openai SDK, base_url 可配
    def __init__(self, base_url, api_key, model, timeout=120): ...
```

- 配置驱动（`config.py`）：`LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_TIMEOUT`，切换厂商只改 env。
- `schema` 参数：要求结构化输出时传 Pydantic 模型，优先用 OpenAI `response_format=json_schema`/工具调用；provider 不支持则退化为「prompt 内嵌 JSON 要求 + 解析校验」。
- `LLMResult` 携带 `content/usage/raw`，落 `script.token_usage` 便于成本观察。

### Prompt 结构（面试导向，防科普水货）

统一骨架 = **角色 + 受众画像 + 任务 + 结构约束 + 输出 schema**。

**planner（主题→计划）**
```
system: 你是资深后端架构师与技术面试官，为一位有 Go 微服务/多租户 SaaS
        实战经验的资深工程师设计通勤学习播客计划。杜绝入门科普，聚焦
        原理、取舍、面试高频考点与生产陷阱。
user:  主题「{topic.title}」，方向 {direction}，拆 {episode_count} 集。
       每集给: 标题 / 3-5 个目标知识点 / 一句话大纲。由浅入深、可被
       面试连环追问的顺序排列。
schema: Plan{ title: str, episodes: list[{seq,title,objectives[],outline}] }
```

**scriptwriter（大纲→脚本）**（五段结构以 01 §3.2 为权威）
```
system: 把单集大纲写成中文播客脚本,面试学习导向,受众是资深工程师,直接上深度。
        结构固定五段(01 §3.2): S0 回顾上集 3 问并给参考要点(首集跳过) →
        S1 核心知识点(深入实现/源码级,讲 why 不讲 what) → S2 高频面试问法
        (2-3 个连环追问,标注考察点) → S3 答题框架(总—分—钩结构,给可复述
        骨架+示范话术) → S4 易错点 + 3 个自测问题(1 概念/1 问法/1 应用)。
        口语化、适合 TTS 朗读、说人话别贴裸代码(代码标识符念语义)、术语用
        通行中文念法。正文按目标字数写(默认约 4500 字 ≈ 15 分钟)。
user:  集标题「{episode.title}」目标 {objectives} 大纲 {outline}
       上集自测题 {recap_questions}(首集为空)
schema: ScriptOut{ content_md, knowledge[], questions[]:
                  {qtype,question,framework[],difficulty} }
```
`questions[]` 末尾 3 题即本集自测题，一并产出直接落题库，避免二次抽取。

### 失败重试与降级

- **重试**：指数退避（1s/2s/4s，上限 `max_attempt`），仅对网络错误/5xx/限流(429)重试；4xx（鉴权/参数）直接 fail。
- **结构化输出修复**：解析失败时，把「上一次输出 + 校验错误」回喂要求重出一次（self-heal），仍失败则该 job failed 并保留 error。
- **降级**：schema 模式不可用时降级为纯文本 + 正则/JSON 提取；LLM 完全不可用时 job 置 failed，前端可手动重试，**绝不阻塞已生成内容的播放**。
- **超时**：单次调用 120s 上限，超时计入 attempt。

---

## 6. 关键技术风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| **Python 3.14 兼容性** | 个别传递依赖 C 扩展无 3.14 wheel，装不上 | requirements 钉小版本并提交 lock；建立 `python -c "import 全量"` 冒烟脚本；预留 pyenv 一键切 3.13 的兜底（配置即兼容，无 3.14 专属语法） |
| **edge-tts 限流/封禁** | 高频调用被微软限流，音频生成失败 | tts 全局信号量限流（默认并发 1，上限 3）+ 请求间隔随机抖动 + 指数退避重试；长脚本**分段合成再合并**（ffmpeg concat），单 chunk 失败只重试该段；音色/限流参数走 config |
| **长任务断点续跑** | 生成中断（关盖/重启）丢失进度 | job 表持久化状态机；进程启动时扫描 `status=running` 重置为 `pending` 重新入队；episode 级幂等——已有 `script`/`audio` 则跳过对应阶段，只补缺失产物；`payload` 携带续跑参数 |
| **LLM 输出不合规** | JSON 解析失败、内容水 | schema 强约束 + self-heal 重出；prompt 内嵌受众画像与结构骨架；planner/scriptwriter 输出落库前做长度与必填字段校验 |
| **音频文件膨胀** | 本地磁盘占用 | 统一码率、`data/audio` 单目录、提供按 plan 清理接口（V2）；episode 删除时级联删文件 |
| **单进程并发** | 边生成边播放互相拖慢 | WAL 模式读写分离；TTS/LLM 全异步不阻塞事件循环；音频播放走 StaticFiles/FileResponse 流式，不占 worker |

---

## 附：关键决策一览

1. **FastAPI + SQLite(WAL) + SQLAlchemy**：轻量、契约强、可平迁 PG。
2. **进程内 asyncio worker + DB job 表**：免 broker，天然断点续跑。
3. **内容生产全部走 job 异步**，web 只触发与查询，播放永不被生成阻塞。
4. **脚本即展示稿即 TTS 稿**，题库随脚本一次产出。
5. **LLM 走 OpenAI 兼容抽象**，env 切厂商，schema 强约束 + self-heal 保质量。
