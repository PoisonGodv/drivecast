"""参考猎手:生成脚本时实时检索参考链接。

来源按新鲜度排序合并,统一过 HTTP 存活校验:
1. 实时搜索 API(tavily/bing,配置 ref_search_provider + ref_search_api_key 启用);
2. LLM 提名候选链接(模型知识里的官方文档/论文页),校验存活后才采用;
3. 题库静态 refs 兜底基线(同样校验;网络整体不可用时 fail-open 保留未校验基线)。

校验策略:404/410/5xx 淘汰;403/429 保留(多为反爬限流,浏览器可开);
连接错误记为网络故障,若网络故障占比过半说明本机断网/代理失效,
此时放弃校验直接保留全部候选(宁可给旧链接,不给空列表)。
本模块永不抛异常:任何环节失败都降级为静态基线。
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import BaseModel, Field

from ..config import settings
from ..llm.provider import OpenAICompatProvider, get_provider
from ..schemas import EpisodeRec, PlanRec, Reference

logger = logging.getLogger(__name__)


class _RefCandidates(BaseModel):
    """LLM 提名输出:只收确信真实存在的稳定链接。"""

    refs: list[Reference] = Field(default_factory=list)


_PROPOSE_SYSTEM = """你是技术资料检索助手,为面试学习播客的一集内容提名权威参考链接。
只提名你确信真实存在的稳定 URL:官方文档落地页、官方博客、arXiv 摘要页(形如 arxiv.org/abs/编号)、
GitHub 仓库或源码文件路径。绝对不要编造具体文章的深链;拿不准就提名该官方文档的栏目落地页。"""


def _propose_user(plan: PlanRec, ep: EpisodeRec, matched: list[dict]) -> str:
    topics = sorted({str(t) for q in matched for t in q.get("topic", []) if t})
    return f"""学习计划:「{plan.title}」(方向 {plan.direction})
本集:「{ep.title}」
目标:{ep.goal}
知识点:{", ".join(ep.objectives)}
涉及题簇:{", ".join(topics)}

请提名 3-6 条参考链接,每条给 title(页面标题)、url、note(一句话说明对应本集哪个考点)。"""


_provider: OpenAICompatProvider | None = None
_client_factory = httpx.AsyncClient  # 测试可替换


def _get_provider() -> OpenAICompatProvider:
    global _provider
    if _provider is None:
        _provider = get_provider()
    return _provider


def _set_provider(provider: OpenAICompatProvider | None) -> None:
    """仅用于测试注入,不暴露给业务代码。"""
    global _provider
    _provider = provider


def baseline_refs(matched: list[dict], cards: list[dict]) -> list[dict]:
    """静态基线:命中题库题的 refs(每题前 2 条)+ 事实卡 url/link,按 url 去重。"""
    refs: list[dict] = []
    seen: set[str] = set()

    def _add(title: object, url: object, note: object = "") -> None:
        url_text = str(url or "").strip()
        if not url_text or url_text in seen:
            return
        seen.add(url_text)
        refs.append({
            "title": str(title or "").strip() or url_text,
            "url": url_text,
            "note": str(note or "").strip(),
        })

    for q in matched:
        for ref in (q.get("refs") or [])[:2]:
            if isinstance(ref, dict):
                _add(ref.get("title"), ref.get("url"), ref.get("note"))
    # 事实卡当前结构无 url/link 字段;防御式保留,将来补字段后自动并入
    for card in cards:
        if card.get("url") or card.get("link"):
            _add(card.get("title"), card.get("url") or card.get("link"))
        for fact in card.get("facts", []):
            if isinstance(fact, dict) and (fact.get("url") or fact.get("link")):
                _add(card.get("title"), fact.get("url") or fact.get("link"),
                     str(fact.get("claim", ""))[:80])
    return refs


# ---------- 来源 1:实时搜索 API ----------

def _search_queries(plan: PlanRec, ep: EpisodeRec, matched: list[dict]) -> list[str]:
    """搜索查询:集标题 + 前两个命中题面,题面比标题更接近可检索的原文。"""
    queries = [ep.title]
    queries.extend(str(q.get("question", "")) for q in matched[:2] if q.get("question"))
    return [q for q in queries if q][:3]


async def _search_web(queries: list[str]) -> list[dict]:
    """调用搜索 API(tavily/bing),返回 [{title,url,note}];未配置或失败返回 []。"""
    provider = settings.ref_search_provider.lower().strip()
    key = settings.ref_search_api_key.strip()
    if not provider or not key:
        return []
    out: list[dict] = []
    async with _client_factory(timeout=settings.ref_verify_timeout) as client:
        for query in queries:
            if provider == "tavily":
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={"query": query, "max_results": 3},
                    headers={"Authorization": f"Bearer {key}"},
                )
                resp.raise_for_status()
                for item in resp.json().get("results", []):
                    out.append({
                        "title": str(item.get("title") or "").strip(),
                        "url": str(item.get("url") or "").strip(),
                        "note": "实时搜索",
                    })
            elif provider == "bing":
                resp = await client.get(
                    "https://api.bing.microsoft.com/v7.0/search",
                    params={"q": query, "count": 3},
                    headers={"Ocp-Apim-Subscription-Key": key},
                )
                resp.raise_for_status()
                for item in resp.json().get("webPages", {}).get("value", []):
                    out.append({
                        "title": str(item.get("name") or "").strip(),
                        "url": str(item.get("url") or "").strip(),
                        "note": "实时搜索",
                    })
    return [item for item in out if item["url"]]


# ---------- 来源 2:LLM 提名 ----------

async def _llm_propose(plan: PlanRec, ep: EpisodeRec, matched: list[dict]) -> list[dict]:
    result = await _get_provider().complete(
        system=_PROPOSE_SYSTEM,
        user=_propose_user(plan, ep, matched),
        schema=_RefCandidates,
        temperature=0.3,
    )
    if result.parsed is None:
        return []
    return [ref.model_dump() for ref in result.parsed.refs if ref.url.strip()]


# ---------- HTTP 存活校验 ----------

# None=网络故障(不计生死),True=存活,False=确认死亡
async def _check(client: httpx.AsyncClient, url: str) -> bool | None:
    try:
        resp = await client.head(url, follow_redirects=True)
        if resp.status_code in (405, 501):  # 服务器不支持 HEAD,降级 GET
            resp = await client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if resp.status_code in (403, 429):  # 反爬/限流:浏览器多半能开,保留
        return True
    if resp.status_code >= 400:  # 404/410/5xx:确认死亡
        return False
    return True


async def _verify(refs: list[dict]) -> list[dict]:
    """并发校验链接存活;网络故障过半时 fail-open 保留全部(防本机断网误杀)。"""
    if not refs:
        return refs
    sem = asyncio.Semaphore(5)

    async def _guarded(client: httpx.AsyncClient, url: str) -> bool | None:
        async with sem:
            return await _check(client, url)

    async with _client_factory(
        timeout=settings.ref_verify_timeout,
        headers={"User-Agent": "Mozilla/5.0 (compatible; drivecast/0.1)"},
    ) as client:
        verdicts = await asyncio.gather(*(_guarded(client, r["url"]) for r in refs))

    network_errors = sum(1 for v in verdicts if v is None)
    if network_errors * 2 > len(verdicts):
        logger.warning(f"网络故障 {network_errors}/{len(verdicts)},放弃校验保留全部候选")
        return refs
    alive = [r for r, v in zip(refs, verdicts) if v is not False]
    dead = len(refs) - len(alive)
    if dead:
        logger.info(f"校验淘汰 {dead} 条死链")
    return alive


# ---------- 编排 ----------

async def hunt_references(
    plan: PlanRec, ep: EpisodeRec, matched: list[dict], cards: list[dict]
) -> list[dict]:
    """集级参考链接主入口。永不抛异常:猎取失败降级为静态基线。"""
    baseline = baseline_refs(matched, cards)
    if not settings.ref_hunter_enabled:
        return baseline[: settings.ref_max_results]

    try:
        fresh: list[dict] = []
        try:
            fresh.extend(await _search_web(_search_queries(plan, ep, matched)))
        except Exception as exc:
            logger.warning(f"{ep.episode_id} 实时搜索失败,跳过: {exc}")
        try:
            fresh.extend(await _llm_propose(plan, ep, matched))
        except Exception as exc:
            logger.warning(f"{ep.episode_id} LLM 提名失败,跳过: {exc}")

        # 合并:实时搜索 > LLM 提名 > 静态基线;按 url 去重
        merged: list[dict] = []
        seen: set[str] = set()
        for ref in [*fresh, *baseline]:
            url = str(ref.get("url", "")).strip()
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append({
                "title": str(ref.get("title") or "").strip() or url,
                "url": url,
                "note": str(ref.get("note") or "").strip(),
            })
        verified = await _verify(merged)
        return verified[: settings.ref_max_results] or baseline[: settings.ref_max_results]
    except Exception as exc:  # 兜底:任何意外都不能击杀了脚本生成
        logger.warning(f"{ep.episode_id} 参考猎取异常,降级静态基线: {exc}")
        return baseline[: settings.ref_max_results]
