from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auth_test_support import authenticated_admin_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivecast import db
from drivecast.config import settings
from drivecast.content import directions
from drivecast.content.loader import _fact_cards_dir
from drivecast.models import Topic
from drivecast.reviewer import prompts as reviewer_prompts
from drivecast.web.main import app


class DirectionsUnitTest(unittest.TestCase):
    def test_fact_cards_are_valid_and_counted_by_direction(self) -> None:
        paths = sorted(_fact_cards_dir().glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            data = json.loads(path.read_text("utf-8"))
            self.assertIn(data.get("direction"), directions.DIRECTION_NAMES, path.name)
            self.assertTrue(data.get("facts"), path.name)
        counts = directions.fact_card_counts()
        self.assertEqual(sum(counts.values()), len(paths))

    def test_directions_flags_without_search(self) -> None:
        with patch.object(settings, "ref_search_provider", ""), patch.object(settings, "ref_search_api_key", ""), \
                patch.object(directions, "fact_card_counts", return_value={"backend_arch": 8}):
            items = {d["key"]: d for d in directions.list_directions()}
        self.assertEqual(set(items), {"backend_arch", "ai_llm", "tech_news", "source_dive"})
        self.assertTrue(items["backend_arch"]["available"])
        self.assertFalse(items["backend_arch"]["experimental"])
        self.assertEqual(items["backend_arch"]["reason"], "")
        for key in ("ai_llm", "source_dive"):
            self.assertTrue(items[key]["available"])
            self.assertTrue(items[key]["experimental"])
            self.assertEqual(items[key]["reason"], "暂无事实卡，审稿缺少依据")
        self.assertFalse(items["tech_news"]["available"])
        self.assertTrue(items["tech_news"]["reason"])

    def test_tech_news_available_when_search_configured(self) -> None:
        with patch.object(settings, "ref_search_provider", "tavily"), patch.object(settings, "ref_search_api_key", "k"):
            self.assertIsNone(directions.unavailable_reason("tech_news"))
        with patch.object(settings, "ref_search_provider", "tavily"), patch.object(settings, "ref_search_api_key", ""):
            self.assertIsNotNone(directions.unavailable_reason("tech_news"))

    def test_reviewer_prompt_states_missing_fact_cards(self) -> None:
        text = reviewer_prompts.reviewer_user({"标题": "t"}, "正文", [])
        self.assertIn("没有事实卡", text)
        self.assertIn("高度确信", text)
        with_cards = reviewer_prompts.reviewer_user({"标题": "t"}, "正文", [{"slug": "x", "facts": []}])
        self.assertNotIn("没有事实卡", with_cards)


class DirectionsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        asyncio.run(db.init_db())
        self.enterContext(patch.object(settings, "ref_search_provider", ""))
        self.enterContext(patch.object(settings, "ref_search_api_key", ""))
        self.client = authenticated_admin_client(self)

    def tearDown(self) -> None:
        asyncio.run(db.close_db())
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    def test_requires_auth(self) -> None:
        from fastapi.testclient import TestClient
        anon = TestClient(app)  # 不进 with,避免触发 lifespan 启动 worker
        self.addCleanup(anon.close)
        self.assertEqual(anon.get("/api/directions").status_code, 401)

    def test_list_directions(self) -> None:
        res = self.client.get("/api/directions")
        self.assertEqual(res.status_code, 200)
        items = {d["key"]: d for d in res.json()}
        self.assertFalse(items["tech_news"]["available"])
        self.assertTrue(items["ai_llm"]["experimental"])
        self.assertEqual(set(items["ai_llm"]), {"key", "label", "available", "experimental", "reason"})

    def test_create_topic_rejects_unavailable_direction(self) -> None:
        res = self.client.post("/api/topics", json={"title": "本周 AI 新闻", "direction": "tech_news"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("暂不可用", res.json()["detail"]["message"])
        ok = self.client.post("/api/topics", json={"title": "RAG", "direction": "ai_llm"})
        self.assertEqual(ok.status_code, 201)

    def test_create_plan_rejects_legacy_unavailable_topic(self) -> None:
        async def seed() -> int:
            async with db.session() as s:
                t = Topic(title="旧资讯主题", direction="tech_news", user_id=1)
                s.add(t)
                await s.commit()
                return t.id

        topic_id = asyncio.run(seed())
        res = self.client.post("/api/plans", json={"topic_id": topic_id})
        self.assertEqual(res.status_code, 409)
        self.assertIn("暂不可用", res.json()["detail"]["message"])

        # 方向不可用(409)先于额度(429):即使同时超额也先告诉用户方向问题
        with patch.object(settings, "quota_exempt_usernames", ""), \
                patch.object(settings, "quota_daily_episode_minutes", 1):
            res = self.client.post("/api/plans", json={"topic_id": topic_id})
            self.assertEqual(res.status_code, 409)
            ok_topic = self.client.post("/api/topics", json={"title": "RAG", "direction": "ai_llm"}).json()
            res = self.client.post("/api/plans", json={"topic_id": ok_topic["id"]})
            self.assertEqual(res.status_code, 429, res.text)


if __name__ == "__main__":
    unittest.main()
