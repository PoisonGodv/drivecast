"""LLM 接入层(docs/02 §5):OpenAI 兼容 + schema 结构化输出 + self-heal 重出。

策略:
1. 请求时要求 JSON(prompt 内嵌 schema 说明 + response_format=json_object 尽力而为,
   provider 不支持自动降级为纯文本)。
2. 从输出中提取 JSON(容错 markdown fence)→ pydantic 校验。
3. 校验失败 → self-heal:把错误回喂要求重出一次,仍失败则抛 LLMOutputError。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field

import openai
from pydantic import BaseModel, ValidationError

from ..config import settings
from .retry import with_retry


class LLMOutputError(Exception):
    """LLM 输出无法解析/校验,且 self-heal 后仍失败。"""


@dataclass
class LLMResult:
    content: str
    parsed: BaseModel | None = None
    usage_tokens: int = 0
    model: str = ""
    extra: dict = field(default_factory=dict)


def extract_json(text: str) -> str:
    """从 LLM 输出里提取 JSON 文本:优先 ```json fence,否则取首个 { 到末个 }。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    raise LLMOutputError(f"输出中找不到 JSON: {text[:200]}...")


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = base_url or settings.llm_base_url
        self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout
        self.idle_timeout = settings.llm_idle_timeout
        if not (self.base_url and (api_key or settings.llm_api_key) and self.model):
            raise RuntimeError("LLM 未配置:请在 .env 设置 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL")
        self._client = openai.AsyncOpenAI(
            base_url=self.base_url,
            api_key=api_key or settings.llm_api_key,
            timeout=self.timeout,
            # 某些代理网关按 UA 拦截 OpenAI SDK,换成通用 UA
            default_headers={"User-Agent": "drivecast/0.1"},
        )

    async def _chat(self, messages: list[dict], *, temperature: float, json_mode: bool) -> LLMResult:
        kwargs: dict = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        async def _call() -> LLMResult:
            async def _raw() -> LLMResult:
                # 流式接收 + 空闲看门狗:长文生成真实耗时可达 100-500s,固定 wall-clock
                # 超时会周期性击杀"慢但活着"的生成(非流式重试再从零开始)。改流式后,
                # 只有 idle_timeout 秒收不到任何 chunk 才视为连接挂死;整体 wall-clock
                # 上限由外层 wait_for(timeout=self.timeout) 兜底(docs/06 §10)。
                try:
                    stream = await self._client.chat.completions.create(
                        model=self.model, messages=messages, temperature=temperature,
                        stream=True, stream_options={"include_usage": True}, **kwargs,
                    )
                except openai.BadRequestError as exc:
                    text = str(exc)
                    if json_mode and "response_format" in text:
                        # provider 不支持 response_format → 降级纯文本重试一次(仍走流式)
                        stream = await self._client.chat.completions.create(
                            model=self.model, messages=messages, temperature=temperature,
                            stream=True,
                        )
                    elif "stream_options" in text:
                        # 网关不认 stream_options → 去掉再开流(usage 拿不到就算 0)
                        stream = await self._client.chat.completions.create(
                            model=self.model, messages=messages, temperature=temperature,
                            stream=True, **kwargs,
                        )
                    else:
                        raise
                parts: list[str] = []
                usage = 0
                it = stream.__aiter__()
                try:
                    while True:
                        chunk = await asyncio.wait_for(it.__anext__(), timeout=self.idle_timeout)
                        if chunk.choices:
                            delta = chunk.choices[0].delta.content
                            if delta:
                                parts.append(delta)
                        if chunk.usage:
                            usage = chunk.usage.total_tokens
                except StopAsyncIteration:
                    pass
                finally:
                    await stream.close()  # AsyncStream.close 是协程;看门狗打断后必须关流防泄漏
                return LLMResult(content="".join(parts), usage_tokens=usage, model=self.model)

            from ..concurrency import request_limit
            async with request_limit("llm"):
                return await asyncio.wait_for(_raw(), timeout=self.timeout)

        return await with_retry(_call, max_attempts=settings.llm_max_retries, what="llm-chat")

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
        temperature: float = 0.7,
    ) -> LLMResult:
        if schema is None:
            return await self._chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature, json_mode=False,
            )

        schema_hint = json.dumps(
            _schema_skeleton(schema), ensure_ascii=False, indent=2
        )
        sys_full = (
            f"{system}\n\n【输出要求】只输出一个 JSON 对象,不要输出任何其他文字、"
            f"不要用 markdown 代码块包裹。JSON 结构如下(字段含义见各字段):\n{schema_hint}"
        )
        result = await self._chat(
            [{"role": "system", "content": sys_full}, {"role": "user", "content": user}],
            temperature=temperature, json_mode=True,
        )
        try:
            result.parsed = schema.model_validate(json.loads(extract_json(result.content)))
            return result
        except (LLMOutputError, json.JSONDecodeError, ValidationError) as exc:
            first_err = str(exc)

        # self-heal:回喂错误,要求重出一次(docs/02 §5)
        heal_user = (
            f"{user}\n\n【修正】你上一次的输出不符合要求,错误:{first_err[:500]}\n"
            f"上一次输出前 500 字:{result.content[:500]}\n请重新输出正确的 JSON。"
        )
        result2 = await self._chat(
            [{"role": "system", "content": sys_full}, {"role": "user", "content": heal_user}],
            temperature=temperature, json_mode=True,
        )
        try:
            result2.parsed = schema.model_validate(json.loads(extract_json(result2.content)))
            return result2
        except (LLMOutputError, json.JSONDecodeError, ValidationError) as exc2:
            raise LLMOutputError(f"self-heal 后仍无法解析: {exc2}") from exc2


def _schema_skeleton(schema: type[BaseModel]) -> dict:
    """给 LLM 看的简化 JSON 骨架(类型名占位),比完整 JSON Schema 更省 token 且更直观。"""
    def walk(model: type[BaseModel]) -> dict:
        out: dict = {}
        for name, f in model.model_fields.items():
            ann = f.annotation
            desc = f.description or ""
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                val: object = walk(ann)
            else:
                val = str(ann).replace("list[", "数组<").replace("]", ">").replace("str", "字符串").replace("int", "整数")
            out[f"{name}({desc})" if desc else name] = val
        return out

    return walk(schema)


_default: OpenAICompatProvider | None = None


def get_provider() -> OpenAICompatProvider:
    global _default
    if _default is None:
        _default = OpenAICompatProvider()
    return _default
