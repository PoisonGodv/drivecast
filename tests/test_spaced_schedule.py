"""遗忘曲线排期与全局覆盖测试:

- 打分阶梯:unknown/fuzzy 固定 1/2 天,mastered 按复习次数走 [1,2,4,7,15,30]
- upsert_script 新题初始 next_review_at 取所在集排期日 09:00(过期/缺失则今天)
- daily_plan 只含今天排期且计划 active 的集
- coverage 的 covered/percent/uncovered_high 计算
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select, update

from drivecast import db, persist
from drivecast.config import settings
from drivecast.content.loader import get_question_bank
from drivecast.library import service
from drivecast.models import Episode, Plan, Question, Script, Topic

_FROZEN = datetime(2026, 8, 20, 9, 0, 0)  # 周四 09:00 整


class SpacedScheduleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        self.old_now = service._now
        self.old_persist_now = persist._now
        settings.data_dir = Path(self.tmp.name)
        service._now = lambda: _FROZEN
        persist._now = lambda: _FROZEN
        await db.init_db()

        async with db.session() as s:
            topic = Topic(title="分布式锁", direction="backend_arch")
            s.add(topic)
            await s.flush()
            plan = Plan(ext_id="plan_active", topic_id=topic.id, title="锁",
                        status="active", episode_count=3)
            cancelled = Plan(ext_id="plan_cancelled", topic_id=topic.id, title="旧计划",
                             status="cancelled", episode_count=1)
            s.add_all([plan, cancelled])
            await s.flush()
            s.add_all([
                Episode(ext_id="ep_today", plan_id=plan.id, seq=1, title="今天的新集",
                        status="ready", scheduled_date="2026-08-20"),
                Episode(ext_id="ep_tomorrow", plan_id=plan.id, seq=2, title="明天的新集",
                        status="pending", scheduled_date="2026-08-21"),
                Episode(ext_id="ep_past", plan_id=plan.id, seq=3, title="过期排期的集",
                        status="pending", scheduled_date="2026-08-01"),
                Episode(ext_id="ep_cancelled", plan_id=cancelled.id, seq=1, title="已取消计划的集",
                        status="pending", scheduled_date="2026-08-20"),
            ])
            await s.flush()
            s.add_all([
                # 今天到期(不算逾期)
                Question(episode_id=1, kp_id="kp", question="due", framework="[]",
                         next_review_at="2026-08-20 08:00:00"),
                # 昨天到期(逾期)
                Question(episode_id=1, kp_id="kp", question="overdue", framework="[]",
                         next_review_at="2026-08-19 09:00:00"),
                # 未排期
                Question(episode_id=1, kp_id="kp", question="new", framework="[]"),
                # 未来排期(不计入任何统计)
                Question(episode_id=2, kp_id="kp", question="future", framework="[]",
                         next_review_at="2026-08-21 09:00:00"),
            ])
            await s.commit()

    async def asyncTearDown(self) -> None:
        await db.close_db()
        settings.data_dir = self.old_data_dir
        service._now = self.old_now
        persist._now = self.old_persist_now
        self.tmp.cleanup()

    async def test_grade_mastered_ladder_by_review_count(self) -> None:
        steps = [1, 2, 4, 7, 15, 30, 30]  # 第 6 次起稳定 30 天
        for index, days in enumerate(steps, start=1):
            graded = await service.grade_question(1, "mastered")
            self.assertEqual(graded["review_count"], index)
            expected = _FROZEN + timedelta(days=days)
            self.assertEqual(
                graded["next_review_at"], expected.strftime("%Y-%m-%d %H:%M:%S"),
                f"mastered 第 {index} 次应 +{days} 天",
            )

    async def test_grade_unknown_fuzzy_fixed_interval(self) -> None:
        unknown = await service.grade_question(2, "unknown")
        self.assertEqual(unknown["next_review_at"], "2026-08-21 09:00:00")
        fuzzy = await service.grade_question(2, "fuzzy")
        self.assertEqual(fuzzy["next_review_at"], "2026-08-22 09:00:00")

    async def test_upsert_script_schedules_first_review_on_episode_date(self) -> None:
        data = {
            "content": "稿", "knowledge": ["锁"], "usage_tokens": 1,
            "references": [{"title": "官方文档", "url": "https://redis.io/docs", "note": ""}],
            "questions": [{"question": "q", "framework": ["a"], "qtype": "concept"}],
        }
        await persist.upsert_script("ep_tomorrow", data)  # 明天排期
        await persist.upsert_script("ep_past", data)      # 排期已过期

        async with db.session() as s:
            future_q = await s.scalar(select(Question).where(Question.episode_id == 2))
            past_q = await s.scalar(select(Question).where(Question.episode_id == 3))
            script = await s.scalar(select(Script).where(Script.episode_id == 2))
        self.assertEqual(future_q.next_review_at, "2026-08-21 09:00:00")
        # 过期排期回退今天 09:00;此刻恰好 09:00 不晚于现在,取 09:00
        self.assertEqual(past_q.next_review_at, "2026-08-20 09:00:00")
        self.assertEqual(json.loads(script.refs)[0]["url"], "https://redis.io/docs")

    async def test_upsert_script_after_nine_uses_now_for_today(self) -> None:
        persist._now = lambda: datetime(2026, 8, 20, 10, 30, 0)  # 今天 09:00 已过
        await persist.upsert_script("ep_past", {
            "content": "稿", "questions": [{"question": "q"}],
        })
        async with db.session() as s:
            q = await s.scalar(select(Question).where(Question.episode_id == 3))
        self.assertEqual(q.next_review_at, "2026-08-20 10:30:00")  # 保证今天能复习

        # 重生成(delete 旧题重建)同样按排期规则起排
        await persist.upsert_script("ep_past", {
            "content": "新稿", "questions": [{"question": "q2"}],
        })
        async with db.session() as s:
            rows = list((await s.scalars(
                select(Question).where(Question.episode_id == 3)
            )).all())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].question, "q2")
        self.assertEqual(rows[0].next_review_at, "2026-08-20 10:30:00")

    async def test_daily_plan_only_today_active_episodes(self) -> None:
        daily = await service.daily_plan()
        self.assertEqual(daily["date"], "2026-08-20")
        titles = [ep["title"] for ep in daily["new_episodes"]]
        self.assertEqual(titles, ["今天的新集"])  # 明天/过期/已取消计划的集都不出现
        ep = daily["new_episodes"][0]
        self.assertEqual(ep["scheduled_date"], "2026-08-20")
        self.assertEqual(ep["direction"], "backend_arch")
        self.assertEqual(ep["listen_status"], "unlistened")
        self.assertEqual(daily["due_count"], 2)      # 到期含逾期
        self.assertEqual(daily["overdue_count"], 1)
        self.assertEqual(daily["new_count"], 1)

    async def test_coverage_counts_covers_and_lists_uncovered_high(self) -> None:
        bank = get_question_bank("backend_arch")
        highs = [q for q in bank if q["frequency"] == "high"]
        covered_ids = [highs[0]["id"], highs[1]["id"]]
        async with db.session() as s:
            await s.execute(
                update(Episode).where(Episode.ext_id == "ep_today")
                .values(covers=json.dumps(covered_ids + ["bogus-id"], ensure_ascii=False))
            )
            await s.commit()

        real_loader = service.get_question_bank
        # 只保留 backend_arch 题库,其余方向视为题库文件缺失,不应输出
        with patch.object(service, "get_question_bank",
                          side_effect=lambda d: real_loader(d) if d == "backend_arch" else []):
            result = await service.coverage()

        self.assertEqual([d["direction"] for d in result["directions"]], ["backend_arch"])
        entry = result["directions"][0]
        self.assertEqual(entry["name"], "后端/架构面试")
        self.assertEqual(entry["total"], len(bank))
        self.assertEqual(entry["covered"], 2)  # bogus-id 不计入
        self.assertEqual(entry["percent"], round(2 * 100 / len(bank)))
        uncovered_ids = [item["id"] for item in entry["uncovered_high"]]
        self.assertNotIn(covered_ids[0], uncovered_ids)
        self.assertNotIn(covered_ids[1], uncovered_ids)
        self.assertLessEqual(len(entry["uncovered_high"]), 20)
        # 按题库原序(高频段内保持文件顺序)
        expected = [q["id"] for q in highs if q["id"] not in covered_ids][:20]
        self.assertEqual(uncovered_ids, expected)


if __name__ == "__main__":
    unittest.main()
