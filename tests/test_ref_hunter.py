from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from drivecast.config import settings
from drivecast.refs import hunter
from drivecast.refs.hunter import _RefCandidates
from drivecast.llm.provider import LLMResult
from drivecast.schemas import EpisodeRec, PlanRec, Reference


def _plan() -> PlanRec:
    return PlanRec(plan_id="plan_t", title="分布式锁", direction="backend_arch", user_topic="锁")


def _ep() -> EpisodeRec:
    return EpisodeRec(
        episode_id="ep_t_01", seq=1, title="Redlock 争议",
        goal="说清 fencing token", objectives=["分布式锁", "redlock"],
    )


def _matched() -> list[dict]:
    return [{
        "id": "ba-0002",
        "question": "Redlock 算法的核心步骤是什么?",
        "topic": ["分布式锁", "redlock"],
        "refs": [
            {"title": "Redis 官方 Redlock 文档", "url": "https://redis.io/redlock"},
            {"title": "Martin 的质疑", "url": "https://martin.kleppmann.com/redlock"},
        ],
    }]


class FakeProvider:
    def __init__(self, response):
        self.response = response

    async def complete(self, **kwargs) -> LLMResult:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """按 URL 路由的 httpx.AsyncClient 桩:值 'neterr' 模拟连接失败,否则为状态码。"""

    verdicts: dict[str, object] = {}
    search_payload: dict = {"results": []}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _respond(self, url: str) -> FakeResponse:
        verdict = self.verdicts.get(url, 200)
        if verdict == "neterr":
            raise httpx.ConnectError("network down")
        return FakeResponse(int(verdict))

    async def head(self, url: str, follow_redirects: bool = True) -> FakeResponse:
        return self._respond(url)

    async def get(self, url: str, **kwargs) -> FakeResponse:
        return self._respond(url)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        return FakeResponse(200, self.search_payload)


class HunterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._saved = (
            settings.ref_hunter_enabled,
            settings.ref_search_provider,
            settings.ref_search_api_key,
            settings.ref_max_results,
        )
        self._factory = hunter._client_factory
        hunter._client_factory = FakeClient
        FakeClient.verdicts = {}
        FakeClient.search_payload = {"results": []}

    def tearDown(self) -> None:
        (settings.ref_hunter_enabled, settings.ref_search_provider,
         settings.ref_search_api_key, settings.ref_max_results) = self._saved
        hunter._client_factory = self._factory
        hunter._set_provider(None)

    # ---------- 静态基线 ----------

    def test_baseline_collects_matched_refs_deduped(self) -> None:
        matched = _matched() + [{
            "id": "ba-0001", "question": "q", "topic": ["redis"],
            "refs": [{"title": "重复链接", "url": "https://redis.io/redlock"}],
        }]
        refs = hunter.baseline_refs(matched, [])
        self.assertEqual([r["url"] for r in refs],
                         ["https://redis.io/redlock", "https://martin.kleppmann.com/redlock"])

    async def test_hunter_disabled_returns_baseline(self) -> None:
        settings.ref_hunter_enabled = False
        hunter._set_provider(FakeProvider(RuntimeError("不应被调用")))
        refs = await hunter.hunt_references(_plan(), _ep(), _matched(), [])
        self.assertEqual(len(refs), 2)

    # ---------- LLM 提名 + 校验 ----------

    async def test_llm_candidates_verified_and_dead_dropped(self) -> None:
        parsed = _RefCandidates(refs=[
            Reference(title="活链", url="https://go.dev/doc"),
            Reference(title="死链", url="https://example.com/gone"),
            Reference(title="反爬", url="https://platform.openai.com/docs"),
        ])
        hunter._set_provider(FakeProvider(LLMResult(content="", parsed=parsed)))
        FakeClient.verdicts = {
            "https://go.dev/doc": 200,
            "https://example.com/gone": 404,
            "https://platform.openai.com/docs": 403,
        }
        refs = await hunter.hunt_references(_plan(), _ep(), _matched(), [])
        urls = [r["url"] for r in refs]
        self.assertIn("https://go.dev/doc", urls)
        self.assertIn("https://platform.openai.com/docs", urls)  # 403 反爬保留
        self.assertNotIn("https://example.com/gone", urls)       # 404 淘汰
        # LLM 提名排在静态基线之前
        self.assertLess(urls.index("https://go.dev/doc"), urls.index("https://redis.io/redlock"))

    async def test_llm_failure_falls_back_to_baseline(self) -> None:
        hunter._set_provider(FakeProvider(RuntimeError("llm down")))
        refs = await hunter.hunt_references(_plan(), _ep(), _matched(), [])
        self.assertEqual([r["url"] for r in refs],
                         ["https://redis.io/redlock", "https://martin.kleppmann.com/redlock"])

    # ---------- 实时搜索 ----------

    async def test_search_results_rank_first(self) -> None:
        settings.ref_search_provider = "tavily"
        settings.ref_search_api_key = "k"
        FakeClient.search_payload = {"results": [
            {"title": "新鲜面经", "url": "https://nowcoder.com/fresh"},
        ]}
        hunter._set_provider(FakeProvider(LLMResult(content="", parsed=_RefCandidates())))
        refs = await hunter.hunt_references(_plan(), _ep(), _matched(), [])
        self.assertEqual(refs[0]["url"], "https://nowcoder.com/fresh")
        self.assertEqual(refs[0]["note"], "实时搜索")

    # ---------- 网络整体故障 fail-open ----------

    async def test_network_outage_keeps_unverified_candidates(self) -> None:
        parsed = _RefCandidates(refs=[Reference(title="A", url="https://a.dev/x")])
        hunter._set_provider(FakeProvider(LLMResult(content="", parsed=parsed)))
        FakeClient.verdicts = {
            "https://a.dev/x": "neterr",
            "https://redis.io/redlock": "neterr",
            "https://martin.kleppmann.com/redlock": "neterr",
        }
        refs = await hunter.hunt_references(_plan(), _ep(), _matched(), [])
        # 网络故障过半 → 放弃校验,候选全保留(不给空列表)
        self.assertEqual(len(refs), 3)


if __name__ == "__main__":
    unittest.main()
