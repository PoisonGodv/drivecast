"""审稿人 prompt(docs/07 §3)。"""
from __future__ import annotations

import json


REVIEWER_SYSTEM = """你是 drivecast 的事实审稿人,任务是在脚本进入语音合成前拦截事实错误。

审查标准:
- hard: 与事实卡直接矛盾,或编造 API/函数名/数字/版本/配置。必须 block。
- soft: 表述可疑、无法仅凭事实卡确认,或可能过度简化导致听众误解。不 block,但要标记。
- soft: 大段展开学习计划主题之外的技术栈(主题在脚本元数据中给出;一句话对比参照不算)。不 block,但要标记。

工作方式:
1. 先逐条列出脚本里所有可核验的 factual claims(技术事实、API/函数名、数字、版本、配置等)。
2. 对照事实卡逐条判定。
3. 输出每条 flag 的原文 quote、类型 hard/soft、原因、修改建议。

注意:
- 不要吹毛求疵:未在事实卡中提到的普通技术常识不必标记。
- quote 必须是脚本中的原文片段,方便人工复核。
- 不要输出任何与 JSON 无关的内容。"""


def reviewer_user(script: dict, fact_cards: list[dict]) -> str:
    """把脚本与事实卡拼成审稿请求。"""
    cards_text = json.dumps(fact_cards, ensure_ascii=False, indent=2) if fact_cards else "(本集未提供事实卡)"
    script_text = json.dumps(script, ensure_ascii=False, indent=2)
    return f"""请审查以下播客脚本的事实准确性。

【事实卡】
{cards_text}

【待审脚本】
{script_text}

请按系统提示中的两步进行:先列出 checkable claims,再给出 verdict 与 flags。"""
