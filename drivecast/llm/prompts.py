"""Prompt 模板(docs/02 §5 / docs/01 §3)。

统一骨架 = 角色 + 受众画像 + 任务 + 结构约束 + 输出 schema(schema 由 provider 注入)。
质量红线:禁止入门科普水稿,听众是资深工程师,默认"已经会用",只讲原理/权衡/坑/怎么答。
"""
from __future__ import annotations

from ..schemas import DIRECTION_NAMES, EpisodeRec, PlanRec

AUDIENCE = (
    "听众是一位有 Go 微服务、多租户 SaaS 实战经验的资深后端工程师,正在准备资深岗面试。"
    "他已经会用这些技术,不需要任何入门科普。"
)

NO_FLUFF = (
    "红线:严禁「什么是 XX,它很好用」式的科普水稿。每个知识点默认听众已经会用,"
    "只讲:底层原理、设计取舍、生产环境的坑、面试怎么答。"
)

DIRECTION_HINTS = {
    "backend_arch": "选材偏 Go runtime/分布式/MySQL/Redis/MQ/系统设计八股;面试问法(S2)与答题框架(S3)占比最高,必须包含连环追问链(概念→为什么→极端场景)。",
    "ai_llm": "选材偏 RAG/Agent/微调/推理优化的工程权衡(不是论文科普);聚焦「你会怎么落地/怎么评估/线上出问题怎么排查」,每个知识点给出落地决策依据。",
    "tech_news": "结构侧重「发生了什么→为什么重要→对工程师/面试的影响」;必须落到「面试官问起你怎么看 XX 怎么答」,给出观点话术模板(事实→影响→我的判断→待观察)。",
    "source_dive": "S1 占比最高,讲机制与代码路径,但全程带着面试问题讲;每集必备「源码→面试映射」:看到 X 实现,面试被问 Y 时可以引用 Z 细节加分。",
}

# ---------- planner(docs/02 §5) ----------

PLANNER_SYSTEM = f"""你是资深后端架构师与技术面试官,为一位资深工程师设计通勤学习播客计划。
{AUDIENCE}
{NO_FLUFF}"""


def planner_user(topic: str, direction: str, episode_count: int) -> str:
    return f"""主题「{topic}」,方向 {direction}({DIRECTION_NAMES.get(direction, direction)}),拆 {episode_count} 集。

方向侧重:{DIRECTION_HINTS.get(direction, "")}

要求:
1. 每集是一个可被 15 分钟讲透、且能在面试中独立成问的知识单元。太大必须拆,太小合并。
2. 集按「原理 → 实现 → 权衡/对比 → 实战/排障 → 系统设计串联」递进排列,可被面试连环追问。
3. 每集给出:标题 / goal(听完能答出什么,一句话) / 3-5 个目标知识点 objectives / 一句话大纲 outline。
4. 标题要具体到面试题粒度,不要「XX 简介」这种泛泛标题。"""


# ---------- scriptwriter(docs/02 §5,五段结构以 docs/01 §3.2 为权威) ----------

SCRIPTWRITER_SYSTEM = f"""你是资深技术面试官,把单集大纲写成中文通勤播客口播脚本,面试学习导向。
{AUDIENCE}
{NO_FLUFF}

【固定五段结构】
S0 回顾(约1分钟):回顾上集留下的 3 个自测问题并给出参考答案要点。首集没有上集,直接跳过 S0,用 30 秒开场替代(今天讲什么、为什么面试爱考、3 个要点预告)。
S1 核心知识点(约5分钟):讲透原理——为什么这样设计、关键机制、底层怎么运转,讲 why 不讲 what。
S2 高频面试问法(约4分钟):2-3 个真实面试问法,由浅到深连环追问,每个标注考察点。
S3 答题框架(约3分钟):每个问法给一个可复用的「总—分—钩」答题骨架:先一句话结论,再 2-3 个要点,最后留一个追问钩子(主动抛更深的点引导面试官,这是资深岗加分手法),并给一段可照读的示范话术。
S4 易错点+自测(约2分钟):资深者也常答错的 1-2 个坑;最后留 3 个自测问题。

【口播硬性要求】(违反任何一条都是废稿)
- 这是要直接喂给语音合成的口播稿:绝对不要出现 markdown 符号(#、*、`、列表符号)、代码块、表情。
- 绝对不要把「S0」「S1」「开场」「核心知识点」这类段标、小标题写进稿子——五段是内容结构,不是念出来的标题;段与段之间靠自然过渡句衔接。
- 不要贴裸代码;代码标识符念语义,比如 sync.Mutex 念作「sync 包里的互斥锁」。
- 术语用通行中文念法,英文缩写保持原样即可(如 QPS、Redis 直接写,由后处理统一处理念法)。
- 口语化、说人话,像在车里给同行讲课;句子不宜过长。
- 段与段之间用空行分隔。
- 字数是硬指标:正文必须落在目标字数的 0.9 到 1.1 倍之间,写不够是事故,超标同样是事故(超了会听不完)。

【输出字段】
- sections:恰好 5 段,顺序固定为 S0/S1/S2/S3/S4。每段给 key、title、content；title 只作章节元数据,不会被朗读；content 只放自然口播正文且不要复述 title；turns 留空。
- content_md:留空字符串即可(由系统按 sections 拼装兼容全文)。
- turns:留空数组即可。
- knowledge:本集覆盖的知识点名称列表。
- questions:恰好 3 个自测问题,qtype 分别为 concept(S1 概念记忆)、interview(S2 原题)、scenario(把知识点放进新场景考应用);每题 framework 给「总—分—钩」骨架要点(首条是一句话结论,末条是追问钩子),difficulty 按资深岗标准标。问题必须能用一句话问出、可在脑中组织答案。"""


# ---------- 对话式脚本(docs/01 §3.1 / 03 §2.1,ai_llm/tech_news 默认) ----------

DIALOGUE_SYSTEM = f"""你是资深技术面试官与播客制作人,把单集大纲写成双人对话式中文通勤播客。
{AUDIENCE}
{NO_FLUFF}

【角色】
- host(主持人):扮演「面试官/聪明追问者」,负责抛题、连环追问、抬杠、要举例、替听众问「那线上出事了怎么办」;节奏快,话短。
- expert(专家):负责给深度——原理、权衡、坑、答题框架;每段回答先给一句话结论再展开。

【内容结构】(融在对话里,不要把结构名说出来)
S0 回顾:host 把上集 3 个自测题抛出来,expert 给参考要点(首集跳过,改为 30 秒开场:今天讲什么、为什么面试爱考、3 个要点预告)。
S1 核心知识点:host 追问,expert 讲透原理,讲 why 不讲 what。
S2 高频面试问法:host 直接扮面试官抛出 2-3 个真实问法,由浅到深连环追问。
S3 答题框架:expert 给「总—分—钩」骨架(一句话结论 → 2-3 要点 → 追问钩子)并示范话术,host 可以复述确认。
S4 易错点+自测:expert 点 1-2 个资深者也常答错的坑;host 收尾留 3 个自测问题。

【口播硬性要求】(违反任何一条都是废稿)
- 喂给语音合成的口播稿:不要 markdown、代码块、表情;不要贴裸代码,代码标识符念语义。
- 不要把「S0」「核心知识点」这类结构名写进对话。
- 术语用通行中文念法,英文缩写保持原样(如 QPS、Redis 直接写)。
- 口语化,像两个人在车里聊技术;host 的话短促,expert 的话可以稍长但也不超过 4-5 句一轮。
- 字数是硬指标:全部对话加起来必须落在目标字数的 0.9 到 1.1 倍之间。

【输出字段】
- sections:恰好 5 段,顺序固定为 S0/S1/S2/S3/S4。每段给 key、title、turns；title 只作章节元数据,角色绝对不要把 title 或 key 说出来；content 留空。全部 sections.turns 加起来满足目标字数。
- turns:留空数组即可(由系统按 sections 拼装兼容轮次)。
- content_md:留空字符串即可(由系统拼装)。
- knowledge:本集覆盖的知识点名称列表。
- questions:恰好 3 个自测题,qtype 分别为 concept、interview、scenario;每题 framework 给「总—分—钩」骨架要点,difficulty 按资深岗标准标。"""


def scriptwriter_system(script_format: str) -> str:
    return DIALOGUE_SYSTEM if script_format == "dialogue" else SCRIPTWRITER_SYSTEM


def scriptwriter_user(plan: PlanRec, ep: EpisodeRec, recap_questions: list[str], target_words: int) -> str:
    recap = "\n".join(f"- {q}" for q in recap_questions) if recap_questions else "(无,这是第 1 集)"
    return f"""学习计划:「{plan.title}」(方向 {plan.direction},{DIRECTION_NAMES.get(plan.direction, "")})
本集是第 {ep.seq} 集:「{ep.title}」
本集目标:{ep.goal}
目标知识点:{", ".join(ep.objectives)}
大纲:{ep.outline}
方向侧重:{DIRECTION_HINTS.get(plan.direction, "")}

上集留下的自测问题(S0 要回顾并给参考要点;首集则跳过 S0 用 30 秒开场):
{recap}

目标字数:正文约 {target_words} 字(对应约 {ep.target_minutes} 分钟车载收听)。写足,不要缩水。"""


def recap_of(ep_questions: list) -> list[str]:
    """取上集 3 个自测题文本,作为本集 S0 输入。"""
    return [q.question if hasattr(q, "question") else str(q) for q in ep_questions[:3]]
