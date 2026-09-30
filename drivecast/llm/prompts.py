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


def planner_user(topic: str, direction: str, episode_count: int, target_minutes: int = 15) -> str:
    return f"""主题「{topic}」,方向 {direction}({DIRECTION_NAMES.get(direction, direction)}),拆 {episode_count} 集。

方向侧重:{DIRECTION_HINTS.get(direction, "")}

要求:
1. 每集是一个可被 {target_minutes} 分钟讲透、且能在面试中独立成问的知识单元。太大必须拆,太小合并。
2. 集按「原理 → 实现 → 权衡/对比 → 实战/排障 → 系统设计串联」递进排列,可被面试连环追问。
3. 每集给出:标题 / goal(听完能答出什么,一句话) / 3-5 个目标知识点 objectives / 一句话大纲 outline。
4. 标题要具体到面试题粒度,不要「XX 简介」这种泛泛标题。
5. 大纲按「未覆盖高频题簇优先」组织:题库地图里标注 [已覆盖] 的题是旧计划讲过的,不要重复主讲。
6. 每集必须输出 covers:从题库地图的题 id 中选取本集能回应考点的题,1-6 个,只选题 id,不要杜撰。
7. 主题红线:所有集必须紧扣主题「{topic}」本身。标题/goal/objectives/covers 一律不得引入主题之外的技术栈
   (例如主题是 MySQL 时,不要把 Redis/Kafka/Go runtime 等其他组件列为知识点或选进 covers;作为一句话对比参照可以,展开成段不行)。
   题库地图与方向侧重只是风格参考,选题永远服从本条;宁可缩小范围,不要为了凑集数扩围到主题外。"""


# ---------- scriptwriter 分段 agent(docs/02 §5,五段结构以 docs/01 §3.2 为权威) ----------
#
# 不再用一个大 prompt 一次写完整集:每段(S0-S4)一个独立 LLM 调用,单段聚焦保证深度。
# 编排层(scriptwriter/service.py)负责并行/串行调度、字数预算切分与拼装。

_SPEECH_RULES = """【口播硬性要求】(违反任何一条都是废稿)
- 这是要直接喂给语音合成的口播稿:绝对不要出现 markdown 符号(#、*、`、列表符号)、代码块、表情。
- 绝对不要把「S0」「S1」「开场」「核心知识点」这类段标、小标题写进稿子——你只负责其中一段,段名是内容结构,不是念出来的标题。
- 不要贴裸代码;代码标识符念语义,比如 sync.Mutex 念作「sync 包里的互斥锁」。
- 术语用通行中文念法,英文缩写保持原样即可(如 QPS、Redis 直接写,由后处理统一处理念法)。
- 口语化、说人话,像在车里给同行讲课;句子不宜过长。
- 段与段之间用空行分隔。
- 字数是硬指标:本段正文必须落在目标字数的 0.9 到 1.1 倍之间,写不够是事故,超标同样是事故(超了会听不完)。"""

# 各段使命:独白式
_MONOLOGUE_MISSIONS = {
    "S0": "本段任务:回顾上集留下的 3 个自测问题,逐题给出参考答案要点(每题先一句话结论,再 2-3 个要点)。"
          "若给定「无,这是第 1 集」,改为约 30 秒开场:今天讲什么、为什么面试爱考、3 个要点预告。",
    "S1": "本段任务:把本集目标知识点讲透原理——为什么这样设计、关键机制、底层怎么运转,讲 why 不讲 what。"
          "这是全片核心段,篇幅最长,每个知识点都要落到「面试被连环追问时能答到哪一层」。",
    "S2": "本段任务:给出 2-3 个真实高频面试问法,由浅到深连环追问,每个问法用一句话点明考察点。"
          "必须覆盖给定的题库高频问法,可改写题面但保留考点。",
    "S3": "本段任务:针对给定的面试问法,各给一个可复用的「总—分—钩」答题骨架:先一句话结论,再 2-3 个要点,"
          "最后留一个追问钩子(主动抛更深的点引导面试官,这是资深岗加分手法),并给一段可照读的示范话术。",
    "S4": "本段任务:结合已讲内容,点出 1-2 个资深工程师也常答错的坑;最后自然收束,留 3 个自测问题。",
}

# 各段使命:对话式(host=面试官/追问者,expert=给深度)
_DIALOGUE_MISSIONS = {
    "S0": "本段任务:host 把上集 3 个自测题抛出来,expert 逐题给参考要点(先一句话结论再展开)。"
          "若给定「无,这是第 1 集」,改为约 30 秒开场:今天讲什么、为什么面试爱考、3 个要点预告。",
    "S1": "本段任务:host 连环追问,expert 把本集目标知识点讲透原理,讲 why 不讲 what。"
          "这是全片核心段,篇幅最长,每个知识点都要落到「面试被连环追问时能答到哪一层」。",
    "S2": "本段任务:host 直接扮面试官抛出 2-3 个真实高频问法,由浅到深连环追问,expert 边答边点明考察点。"
          "必须覆盖给定的题库高频问法,可改写题面但保留考点。",
    "S3": "本段任务:expert 针对给定的面试问法各给一个「总—分—钩」答题骨架(一句话结论 → 2-3 要点 → 追问钩子)"
          "并给可照读的示范话术,host 可以复述确认、替听众追问「那线上出事了怎么办」。",
    "S4": "本段任务:expert 结合已讲内容点 1-2 个资深者也常答错的坑;host 收尾,留 3 个自测问题。",
}

_DIALOGUE_ROLES = """【角色】
- host(主持人):扮演「面试官/聪明追问者」,负责抛题、连环追问、抬杠、要举例、替听众问「那线上出事了怎么办」;节奏快,话短。
- expert(专家):负责给深度——原理、权衡、坑、答题框架;每段回答先给一句话结论再展开。
对话要像两个人在车里聊技术:host 的话短促,expert 的话可以稍长但也不超过 4-5 句一轮。"""

_QUIZ_FIELD = (
    "\n- questions:恰好 3 个自测题,qtype 分别为 concept(概念记忆)、interview(本段相关的面试原题)、"
    "scenario(把知识点放进新场景考应用);每题 framework 给「总—分—钩」骨架要点(首条是一句话结论,末条是追问钩子),"
    "difficulty 按资深岗标准标。问题必须能用一句话问出、可在脑中组织答案。"
)


def section_system(script_format: str, key: str, *, quiz: bool = True) -> str:
    """单段 agent 的 system prompt:角色 + 本段使命 + 口播红线 + 输出字段。

    quiz=False 用于 S4 被拆成多部分时的非末段:不出自测题(schema 也不含该字段)。
    """
    if script_format == "dialogue":
        head = "你是资深技术面试官与播客制作人,负责双人对话式中文通勤播客脚本的一个段落,面试学习导向。"
        missions = _DIALOGUE_MISSIONS
        output = ("【输出字段】\n- title:本段标题,只作章节元数据,不会被朗读,角色也绝对不要把它说出来。\n"
                  "- turns:本段对话轮次,speaker 为 host 或 expert,text 为自然口播台词。\n"
                  "- content:留空字符串。")
        role = _DIALOGUE_ROLES
    else:
        head = "你是资深技术面试官,负责中文通勤播客口播脚本的一个段落,面试学习导向。"
        missions = _MONOLOGUE_MISSIONS
        output = ("【输出字段】\n- title:本段标题,只作章节元数据,不会被朗读。\n"
                  "- content:本段自然口播正文,不要复述 title。\n"
                  "- turns:留空数组。")
        role = ""
    quiz_field = _QUIZ_FIELD if key == "S4" and quiz else ""
    parts = [head, AUDIENCE, NO_FLUFF, role, missions[key], _SPEECH_RULES, output + quiz_field]
    return "\n\n".join(p for p in parts if p)


def section_user(plan: PlanRec, ep: EpisodeRec, key: str, target_words: int, *,
                 recap_questions: list[str] | None = None,
                 s2_grounding: str = "", context: str = "", part_note: str = "") -> str:
    """单段 agent 的 user prompt:全集上下文 + 本段专属输入 + 字数预算。"""
    parts = [f"""学习计划:「{plan.title}」(方向 {plan.direction},{DIRECTION_NAMES.get(plan.direction, "")})
用户主题:「{plan.user_topic}」——主题红线:所有内容必须紧扣该主题;主题外的技术栈只能作一句话对比参照,禁止展开成段。
本集是第 {ep.seq} 集:「{ep.title}」
本集目标:{ep.goal}
目标知识点:{", ".join(ep.objectives)}
大纲:{ep.outline}
方向侧重:{DIRECTION_HINTS.get(plan.direction, "")}(方向画像仅定风格,选题服从上面的主题红线)"""]
    if key == "S0":
        recap = ("\n".join(f"- {q}" for q in recap_questions)
                 if recap_questions else "(无,这是第 1 集,写 30 秒开场)")
        parts.append(f"上集留下的自测问题(逐题回顾并给参考要点):\n{recap}")
    if s2_grounding:
        parts.append(s2_grounding)
    if context:
        parts.append(f"【已写好的相邻段落,保持衔接与口径一致,不要重复其内容】\n{context}")
    if part_note:
        parts.append(f"【分工】{part_note}")
    parts.append(f"本段目标字数:约 {target_words} 字。写足,不要缩水。")
    return "\n\n".join(parts)


def split_part_note(key: str, index: int, parts: int, per_words: int,
                    scope: list[str], scope_style: str) -> str:
    """长段拆分时给第 index 个子 agent 的分工说明(40 分钟长集:单段超出单 agent 字数上限)。

    scope_style: objectives=按知识点切片(S1) / questions=按题库问法切片(S2) /
    order=按上下文问法顺序均分(S3);空=只给接力位置约束。
    """
    lines = [
        f"本段篇幅较长,由 {parts} 个部分接力合写,你负责第 {index + 1}/{parts} 部分"
        f"(约 {per_words} 字),只写自己这部分,完成后会自动拼接成完整一段,不要写段标或部分编号。",
    ]
    items = [str(item).strip() for item in scope if str(item).strip()]
    if items and scope_style == "objectives":
        lines.append("你只负责讲透以下知识点,其余知识点由其他部分负责,绝对不要提前展开:\n"
                     + "\n".join(f"- {item}" for item in items))
    elif items and scope_style == "questions":
        lines.append("本部分必须覆盖以下题库高频问法(至少 1 题,可改写题面但保留考点),"
                     "其余问法由其他部分负责,不要越界:\n"
                     + "\n".join(f"- {item}" for item in items))
    elif scope_style == "order":
        lines.append(f"把给定上下文中的问法按出现顺序均分为 {parts} 组,"
                     f"你只写第 {index + 1} 组问法的答题框架,其他组不要写。")
    if index == 0:
        lines.append("你是第一部分:直接从正题开始,不要写「承接上文」式过渡;"
                     "讲到你的内容结束自然停住,不要总结收尾。")
    elif index == parts - 1:
        lines.append("你是最后一部分:不要写开场寒暄,不要重复前面部分已讲的内容,"
                     "负责把本段讲到自然收尾。")
    else:
        lines.append("你是中间部分:不要写开场寒暄和承上启下的废话,"
                     "直接进入分配给你的内容,也不要总结收尾。")
    return "\n".join(lines)


def recap_of(ep_questions: list) -> list[str]:
    """取上集 3 个自测题文本,作为本集 S0 输入。"""
    return [q.question if hasattr(q, "question") else str(q) for q in ep_questions[:3]]
