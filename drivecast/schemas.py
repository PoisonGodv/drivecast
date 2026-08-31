"""Pydantic 契约:LLM 结构化输出 + 文件产物 schema。

字段口径以 docs/01 §2/§4 为权威。M1 落文件,M2 同源落 DB(schema 复用)。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Direction = Literal["backend_arch", "ai_llm", "tech_news", "source_dive"]

DIRECTION_NAMES: dict[str, str] = {
    "backend_arch": "后端/架构面试",
    "ai_llm": "AI/LLM 工程",
    "tech_news": "前沿技术资讯",
    "source_dive": "源码/原理深读",
}


# ---------- planner 输出 ----------

class EpisodeOutline(BaseModel):
    seq: int
    title: str
    goal: str = ""                      # 本集目标(能答出什么)
    objectives: list[str] = Field(default_factory=list)  # 3-5 个目标知识点
    outline: str = ""                   # 一句话大纲


class PlanOut(BaseModel):
    title: str
    episodes: list[EpisodeOutline]


# ---------- scriptwriter 输出 ----------

class Question(BaseModel):
    """面试题(docs/01 §4)。answer_framework 是可复述骨架:总—分—钩。"""

    qtype: Literal["concept", "interview", "scenario"] = "concept"
    question: str
    framework: list[str] = Field(default_factory=list)  # 骨架要点,首条为一句话结论
    difficulty: Literal["easy", "medium", "hard"] = "medium"


class Turn(BaseModel):
    """对话式脚本的一轮(docs/03 §2.1):host=引导/追问,expert=讲原理/给框架。"""

    speaker: Literal["host", "expert"]
    text: str


class ScriptSection(BaseModel):
    """不朗读的五段结构源。

    monologue 用 content；dialogue 用 turns。section key/title 只用于章节与元数据，
    scriptwriter 会在保存前拼成兼容旧接口的 content_md/turns，不把段名混入口播。
    """

    key: Literal["S0", "S1", "S2", "S3", "S4"]
    title: str = ""
    content: str = ""
    turns: list[Turn] = Field(default_factory=list)


class ScriptOut(BaseModel):
    """五段式脚本(docs/01 §3.2):S0 回顾→S1 知识点→S2 问法→S3 框架→S4 易错+自测。

    content_md 是纯口播文本(无 markdown/代码块),独白式直接喂 TTS。
    对话式额外给 turns(按轮分说话人),TTS 按 speaker 切音色;content_md 仍给全文供展示。
    questions 末尾 3 题即本集自测题(concept/interview/scenario 各一)。
    """

    content_md: str = ""               # 兼容字段；有 sections 时由系统拼装
    knowledge: list[str] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    turns: list[Turn] = Field(default_factory=list)  # 兼容字段；有 sections 时由系统拼装
    sections: list[ScriptSection] = Field(default_factory=list)  # M6 章节的稳定结构源


# ---------- 文件产物(过程态) ----------

class EpisodeRec(BaseModel):
    """plan 文件里的单集记录。"""

    episode_id: str
    seq: int
    title: str
    goal: str = ""
    objectives: list[str] = Field(default_factory=list)
    outline: str = ""
    depends_on: list[str] = Field(default_factory=list)
    target_minutes: int = 15
    script_format: Literal["monologue", "dialogue"] = "monologue"
    status: Literal["pending", "scripted", "tts_ready", "synth_partial", "ready", "failed"] = "pending"


class PlanRec(BaseModel):
    """data/plans/{plan_id}.json 的格式(docs/01 §2.2 裁剪版)。"""

    plan_id: str
    title: str
    direction: str
    user_topic: str
    level: str = "senior"
    target_minutes: int = 15
    created_at: str = ""
    episodes: list[EpisodeRec] = Field(default_factory=list)
