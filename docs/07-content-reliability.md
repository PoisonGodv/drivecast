# 07 · 内容可靠性与有用性(实施契约)

> 状态:**已实施并通过 46 项测试**。本文档是题库 grounding / 审稿 pass / 事实卡 / 人审 checklist / 勘误再生成五件套的**实现契约**。改实现前先改这里的契约。
> 原则:**可靠**(事实错误拦截在 TTS 之前)与**有用**(命中真实面试分布)分开解决;残余错误靠可见的纠错机制消化,不追求零错误。

## 0. 分工与文件所有权(并行阶段不可越界)

| 包 | 文件 | 归属 |
|---|---|---|
| 题库+grounding | `drivecast/content/`(loader+question_bank)、`planner/service.py`、`scriptwriter/service.py`、`schemas.py`、`tests/test_question_bank.py` | Agent A |
| 审稿模块 | `drivecast/reviewer/`、`tests/test_reviewer.py` | Agent B(不接线主管线) |
| 事实卡+checklist | `drivecast/content/fact_cards/*.json`、`docs/08-qc-checklist.md` | Agent D(不改 .py) |
| 整合 | `config.py`、`scriptwriter/service.py`、`jobs/`、`web/api_plans.py`、`library/artifacts.py`、`library/service.py`、`static/js/main.js`、`static/js/api.js`、`static/index.html` | 整合阶段(单写者) |

## 1. 内容资产包 `drivecast/content/`

> `data/` 在 .gitignore 里,内容资产必须落在包内随 git 走。

### 1.1 题库 `question_bank/<direction>.json`

```json
{
  "direction": "backend_arch",
  "version": 1,
  "updated_at": "2026-09-01",
  "questions": [
    {"id": "ba-0001", "question": "Redlock 争议的核心论点是什么?",
     "topic": ["分布式锁", "redlock"], "frequency": "high",
     "source": "牛客2026面经", "notes": " fencing token 必问"}
  ]
}
```

- 方向键与现有 `Direction` 枚举一致;`frequency ∈ high|mid|low`。
- 每方向 ≥40 题;来源标注真实(可用检索佐证),不确定标 `notes` 待复核。

### 1.2 事实卡 `fact_cards/<slug>.json`

```json
{
  "slug": "distributed-lock-redlock",
  "title": "分布式锁与 Redlock",
  "direction": "backend_arch",
  "version": 1,
  "facts": [
    {"id": "f01", "claim": "SETNX+过期需一条 SET k v NX PX 原子完成",
     "detail": "分两步在 SETNX 与 EXPIRE 间宕机会留下永不过期的死键",
     "source": "redis.io SET 文档", "confidence": 1.0}
  ]
}
```

- 每张 ≥8 条 facts;`confidence < 1.0` 表示待人工复核,source 不许编造。

### 1.3 loader(`drivecast/content/loader.py`)

同步、无 DB、无 LLM:`get_question_bank(direction) -> list[dict]`(按 frequency 排序)、`get_fact_cards(topic_keywords) -> list[dict]`(关键词模糊匹配,空目录返回空列表,不抛错)。

## 2. 题库 grounding(Agent A)

- **planner**:生成大纲的 prompt 注入该方向高频题 top15,要求分集覆盖题簇(大纲 = 题簇的递进组织);主题红线约束所有集紧扣用户主题,不得把主题外技术栈列为知识点/covers。
- **scriptwriter**:S2 高频面试问法的 prompt 注入与当前集主题关键词匹配的题(题面可改写但考点不变)。主题约束:
  - 匹配文本包含 `plan.user_topic`(用户原始主题优先于大纲泛化后的字段);
  - 题库 topic 词做裸 substring 匹配时跳过 <3 字符的过短词(io/db/go 等误命中率极高);
  - 匹配不足 2 题时,兜底只补「topic 词命中用户主题原文」的方向高频题;无相关题则不凑数——禁止把主题外高频题(如 MySQL 主题下的 Redis 题)硬注入 S2。
- schema 兼容:旧字段一律不动,只增不改(`s2` 如需标注题库来源,新增可选字段)。

## 3. 审稿 pass(Agent B 造模块,整合阶段接线)

- `drivecast/reviewer/service.py`:
  `async review_script(script: dict, fact_cards: list[dict]) -> ReviewResult`
- `ReviewResult`(pydantic):`verdict: pass|block`、`flags: [{quote, type: hard|soft, reason, suggestion}]`、`reviewer_model`、`reviewed_at`。
- 判定规则:**≥1 条 hard → block**。hard=与事实卡矛盾 / 编造 API、函数名、数字;soft=表述可疑但不确凿。
- LLM 调用照抄 scriptwriter 的结构化输出模式(读 `llm/provider.py` 后对齐);**解析失败抛 `ReviewerError`**,模块不决定 fail-open/fail-close(整合阶段定)。
- **不接线主管线**;单元测试全部用 fake provider。

### 3.1 管线接入(整合阶段)

- `config.py`:`reviewer_enabled: bool = True`(env `REVIEWER_ENABLED`)。
- scriptwriter 生成后调 reviewer(事实卡按 script 主题选),结果写 `script["review"]`;`block` → 走既有 job 失败路径(episode failed,可重试)。
- **fail-open**:reviewer 自身异常(LLM 超时/解析失败)时放行,但在 `script["review"]` 记 `pending_error` 留痕——生成可用性优先。
- `/api/episodes/{id}/script` 透传 `review` 字段;前端对 `soft>0` 显示 ⚠️。
- **不**升音频管线版本、**不**触发存量集重生成(纯增量,旧脚本无 review 字段照常可读)。

## 4. 人审 checklist 与 QC 状态

- `docs/08-qc-checklist.md`:发布前真听 checklist(术语念法抽查 / 数字与 API 名核对 / 权衡自洽 / S2 靘实来自面经 / 三问可区分度 / senior 深度)+ 记录模板。
- meta JSON 可选字段 `human_qc: {status: pending|passed, checked_at, notes}`(整合阶段加只读展示,写入口本期只做 API)。

## 5. 勘误与再生成(整合阶段)

- `POST /api/episodes/{id}/regenerate`,body 可选 `{reason}`:
  1. 旧产物改名备份(`.mp3`/`.script.json`/`.tts.json`/`meta` → 同目录 `.bak` 后缀,不删除);
  2. episode 状态重置 pending,复用既有 generate 入队路径(script→tts 幂等续跑天然支持);
  3. `reason` 追加进勘误历史。
- **勘误 sidecar(实现修订)**:勘误落独立文件 `data/meta/{ep}.errata.json` 而非内嵌 meta——meta 会随重生成被备份替换,勘误历史必须独立存活。文件操作集中在 `drivecast/library/artifacts.py`。
- 前端:计划详情 ready 行加"重新生成"(弹确认+原因,`⟳` 按钮),失败行原有"重试"不变。
- DB 不动:无新表无迁移,一切落文件态。

## 6. 验收标准(全绿才算完)

1. `.venv/bin/python -m unittest discover -s tests` 全绿(含新增 ≥2 个测试文件)。
2. 题库两方向各 ≥40 题合法 JSON;事实卡 ≥8 张各 ≥8 条。
3. mock 全链路测试:script 生成 → review block → job failed → 重试路径通。
4. `WORKER_ENABLED=false` 冷启动:既有 API 全部不回归(Range 206 / script 200 带 review 透传 / regenerate 对不存在 id 404)。
5. 存量 8 集音频与旧脚本零改动(纯增量验证)。

## 7. 边界

- 不动 `.env`、不真实调用 LLM/edge-tts(测试一律 mock,禁止跑 smoke.py / plan / episode)。
- 不 git commit(由主线审后统一提交)。
- 不引入新第三方依赖。
