"""内容可靠性整合测试(docs/07 §6):
- write_script 审稿接线:hard 拦截 / soft 放行 / ReviewerError fail-open / 开关关闭
- worker 全链路:script 生成 → review block → job 重试 → 最终 failed + episode failed
- API:regenerate 备份+入队+勘误 / qc 人审标记 / script 透传 review+errata
全部 mock LLM,绝不真实调用。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from auth_test_support import authenticated_admin_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivecast import db
from drivecast.config import settings
from drivecast.library import artifacts
from drivecast.llm.provider import LLMResult
from drivecast.models import Episode, Job, Plan, Script, Topic
from drivecast.planner import service as planner_service
from drivecast.reviewer import service as reviewer_service
from drivecast.schemas import (
    EpisodeRec, PlanRec, Question, QuizSectionOut, ScriptOut, ScriptSection, SectionOut,
)
from drivecast.scriptwriter import service as script_service
from drivecast.scriptwriter.service import ScriptBlockedError
from drivecast.web.main import app


def _fake_script_out() -> ScriptOut:
    """最小合法五段脚本(独白式)+3 题。"""
    return ScriptOut(
        sections=[
            ScriptSection(key="S0", title="回顾", content="回顾内容"),
            ScriptSection(key="S1", title="知识点", content="知识点内容"),
            ScriptSection(key="S2", title="问法", content="问法内容"),
            ScriptSection(key="S3", title="框架", content="框架内容"),
            ScriptSection(key="S4", title="易错", content="易错内容"),
        ],
        questions=[
            Question(qtype="concept", question="c", framework=["a"], difficulty="medium"),
            Question(qtype="interview", question="i", framework=["b"], difficulty="medium"),
            Question(qtype="scenario", question="s", framework=["c"], difficulty="medium"),
        ],
    )


class FakeProvider:
    """截获 LLM 调用,返回预设结构化输出。

    scriptwriter 已拆成分段 agent:schema 为 SectionOut/QuizSectionOut 时
    自动把预设的整集 ScriptOut 折算成单段输出;其余调用原样返回 parsed。
    """

    def __init__(self, parsed: object) -> None:
        self.parsed = parsed

    async def complete(self, *, system: str, user: str, schema: type | None = None,
                       temperature: float = 0.7) -> LLMResult:
        parsed = self.parsed
        if isinstance(parsed, ScriptOut) and schema is not None and issubclass(schema, SectionOut):
            content = "分布式锁与 Redlock 的考点讲解,含原理、问法与答题框架。" * 2
            if issubclass(schema, QuizSectionOut):
                parsed = QuizSectionOut(title="易错", content=content, questions=parsed.questions)
            else:
                parsed = SectionOut(title="段落", content=content)
        return LLMResult(content="", parsed=parsed, usage_tokens=10, model="fake")


class RaisingProvider:
    """模拟 LLM 超时/异常,触发 ReviewerError 的 fail-open 路径。"""

    async def complete(self, *, system: str, user: str, schema: type | None = None,
                       temperature: float = 0.7) -> LLMResult:
        raise TimeoutError("simulated reviewer timeout")


def _flag(flag_type: str) -> reviewer_service.Flag:
    return reviewer_service.Flag(
        quote="SETNX 可以不加过期时间", type=flag_type,
        reason="与事实卡矛盾:必须 SET k v NX PX 原子完成",
        suggestion="改为 SET key value NX PX ttl",
    )


def _review_raw(flag_type: str) -> reviewer_service._ReviewRaw:
    return reviewer_service._ReviewRaw(verdict="block", flags=[_flag(flag_type)])


# ---------- write_script 审稿接线 ----------

class WriteScriptReviewTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        self.old_reviewer = settings.reviewer_enabled
        settings.reviewer_enabled = True
        # 参考猎手走真实 LLM/网络,测试里关掉,不影响审稿接线断言
        self.old_ref_hunter = settings.ref_hunter_enabled
        settings.ref_hunter_enabled = False

    async def asyncTearDown(self) -> None:
        settings.data_dir = self.old_data_dir
        settings.reviewer_enabled = self.old_reviewer
        settings.ref_hunter_enabled = self.old_ref_hunter
        reviewer_service._set_provider(None)
        self.tmp.cleanup()

    @staticmethod
    def _plan_ep() -> tuple[PlanRec, EpisodeRec]:
        plan = PlanRec(plan_id="plan_rev", title="分布式锁与 Redlock 争议",
                       direction="backend_arch", user_topic="分布式锁", episodes=[])
        ep = EpisodeRec(episode_id="ep_rev_01", seq=1, title="Redis 分布式锁与 Redlock",
                        goal="g", objectives=["redlock"], outline="o")
        return plan, ep

    async def _write(self) -> tuple[dict, AsyncMock]:
        plan, ep = self._plan_ep()
        upsert = AsyncMock()
        with patch("drivecast.scriptwriter.service.get_provider",
                   return_value=FakeProvider(_fake_script_out())), \
             patch("drivecast.scriptwriter.service.upsert_script", new=upsert):
            data = await script_service.write_script(plan, ep)
        return data, upsert

    async def test_hard_flag_blocks_before_save(self) -> None:
        """hard 硬伤:脚本不落盘、不双写 DB,job 层走重试。"""
        reviewer_service._set_provider(FakeProvider(_review_raw("hard")))
        with self.assertRaises(ScriptBlockedError):
            await self._write()
        self.assertFalse(settings.script_path("ep_rev_01").exists())

    async def test_soft_flag_passes_with_review_attached(self) -> None:
        """soft 标记:放行但随脚本落盘,前端可显示 ⚠️。"""
        reviewer_service._set_provider(FakeProvider(_review_raw("soft")))
        data, upsert = await self._write()
        self.assertEqual(data["review"]["verdict"], "pass")
        self.assertEqual(len(data["review"]["flags"]), 1)
        self.assertEqual(data["review"]["flags"][0]["type"], "soft")
        upsert.assert_awaited_once()
        self.assertTrue(settings.script_path("ep_rev_01").exists())

    async def test_reviewer_error_fails_open_with_trace(self) -> None:
        """审稿 LLM 异常:fail-open 放行,pending_error 留痕。"""
        reviewer_service._set_provider(RaisingProvider())
        data, _ = await self._write()
        self.assertEqual(data["review"]["verdict"], "pass")
        self.assertTrue(data["review"]["pending_error"])
        self.assertTrue(settings.script_path("ep_rev_01").exists())

    async def test_disabled_skips_review_entirely(self) -> None:
        settings.reviewer_enabled = False
        reviewer_service._set_provider(RaisingProvider())  # 若误调会炸,恰好验证未接线
        data, _ = await self._write()
        self.assertNotIn("review", data)


class SequenceReviewer:
    """按调用次序返回预设审稿结果。"""

    def __init__(self, *raws) -> None:
        self.raws = list(raws)
        self.calls = 0

    async def complete(self, **kwargs) -> LLMResult:
        raw = self.raws[min(self.calls, len(self.raws) - 1)]
        self.calls += 1
        return LLMResult(content="", parsed=raw, usage_tokens=1, model="fake")


class SectionDraftTest(unittest.IsolatedAsyncioTestCase):
    """写稿按段落盘:审稿拦截只重写涉事段及其下游,job 重试从草稿续写。"""

    BAD = "SETNX 可以不加过期时间"

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(settings, "data_dir", Path(self.tmp.name)),
            patch.object(settings, "reviewer_enabled", True),
            patch.object(settings, "ref_hunter_enabled", False),
            patch("drivecast.scriptwriter.service.upsert_script", new=AsyncMock()),
            patch.object(script_service, "_gen_section_checked", side_effect=self._section),
            patch.object(script_service, "_gen_section_split", side_effect=self._section),
        ]
        for p in self.patches:
            p.start()
        self.calls: list[tuple[str, str]] = []
        self.bad_keys = {"S1"}
        self.stubborn = False

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        reviewer_service._set_provider(None)
        self.tmp.cleanup()

    async def _section(self, plan, ep, key, target, *, fix_note="", **kwargs):
        self.calls.append((key, fix_note))
        text = f"这是第{key[1]}段的正文内容。" + (
            self.BAD if key in self.bad_keys and (self.stubborn or not fix_note) else "")
        questions = _fake_script_out().questions if key == "S4" else []
        return ScriptSection(key=key, title=key, content=text), questions, 1

    async def _write(self) -> dict:
        plan, ep = WriteScriptReviewTest._plan_ep()
        return await script_service.write_script(plan, ep)

    async def test_block_rewrites_only_offending_section_and_dependents(self) -> None:
        reviewer = SequenceReviewer(_review_raw("hard"),
                                    reviewer_service._ReviewRaw(verdict="pass", flags=[]))
        reviewer_service._set_provider(reviewer)
        data = await self._write()

        rewritten = [k for k, _ in self.calls[5:]]
        self.assertEqual(sorted(rewritten), ["S1", "S4"])  # S4 依赖 S1,一并重写
        fix = dict(self.calls[5:])["S1"]
        self.assertIn(self.BAD, fix)
        self.assertNotIn(self.BAD, data["content"])
        self.assertEqual(data["review"]["verdict"], "pass")
        self.assertFalse(settings.script_draft_path("ep_rev_01").exists())

    async def test_job_retry_resumes_from_draft(self) -> None:
        reviewer_service._set_provider(SequenceReviewer(_review_raw("hard")))
        self.stubborn = True
        # 修正后仍被拦截 → 抛出,草稿保留未涉事段
        with self.assertRaises(ScriptBlockedError):
            await self._write()
        self.assertFalse(settings.script_path("ep_rev_01").exists())
        self.assertTrue(settings.script_draft_path("ep_rev_01").exists())

        self.calls.clear()
        self.stubborn = False
        reviewer_service._set_provider(SequenceReviewer(
            reviewer_service._ReviewRaw(verdict="pass", flags=[])))
        data = await self._write()
        self.assertEqual(sorted(k for k, _ in self.calls), ["S1", "S4"])
        self.assertIn(self.BAD, dict(self.calls)["S1"])  # 续写涉事段携带修正说明
        self.assertEqual(data["review"]["verdict"], "pass")

    async def test_unlocatable_flag_rewrites_everything(self) -> None:
        flag = reviewer_service.Flag(quote="稿子里没有这句", type="hard", reason="r", suggestion="s")
        reviewer_service._set_provider(SequenceReviewer(
            reviewer_service._ReviewRaw(verdict="block", flags=[flag]),
            reviewer_service._ReviewRaw(verdict="pass", flags=[])))
        await self._write()
        self.assertEqual(sorted(k for k, _ in self.calls[5:]), ["S0", "S1", "S2", "S3", "S4"])


# ---------- API:regenerate / qc / script 透传 ----------

class RegenerateQcApiTest(unittest.TestCase):
    def setUp(self) -> None:
        asyncio.run(db.close_db())  # 丢弃指向旧目录的引擎
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        asyncio.run(db.init_db())
        self._write_file(settings.script_path("ep_reg"), {"episode_id": "ep_reg", "content": "旧稿"})
        self._write_file(settings.meta_path("ep_reg"), {"pipeline_version": 7})
        audio = settings.episode_audio_path("ep_reg")
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"ID3old-audio")
        asyncio.run(self._seed())
        self.client = authenticated_admin_client(self)

    def tearDown(self) -> None:
        asyncio.run(db.close_db())
        settings.data_dir = self.old_data_dir
        self.tmp.cleanup()

    @staticmethod
    def _write_file(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")

    async def _seed(self) -> None:
        async with db.session() as s:
            topic = Topic(title="勘误", direction="backend_arch")
            s.add(topic)
            await s.flush()
            plan = Plan(ext_id="plan_reg", topic_id=topic.id, title="勘误计划",
                        status="active", episode_count=1)
            s.add(plan)
            await s.flush()
            episode = Episode(ext_id="ep_reg", plan_id=plan.id, seq=1,
                               title="待勘误集", status="ready")
            s.add(episode)
            await s.flush()
            s.add(Script(episode_id=episode.id, content_md="# 旧稿", knowledge='["k1"]'))
            await s.commit()
            self.episode_db_id = episode.id

    async def _fetch(self, model, **where):
        from sqlalchemy import select
        async with db.session() as s:
            stmt = select(model)
            for key, value in where.items():
                stmt = stmt.where(getattr(model, key) == value)
            return await s.scalar(stmt)

    def test_regenerate_backs_up_errata_and_enqueues(self) -> None:
        resp = self.client.post(f"/api/episodes/{self.episode_db_id}/regenerate",
                                json={"reason": "术语念错"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "pending")

        # 产物改名 .bak,原件让位给重生成
        for path in (settings.script_path("ep_reg"), settings.meta_path("ep_reg"),
                     settings.episode_audio_path("ep_reg")):
            self.assertFalse(path.exists(), f"{path} 应已改名")
            self.assertTrue(Path(f"{path}.bak").exists(), f"{path}.bak 应存在")

        # 勘误历史落 sidecar
        errata = artifacts.load_errata("ep_reg")
        self.assertEqual(len(errata), 1)
        self.assertEqual(errata[0]["reason"], "术语念错")

        # 状态重置 + script job 入队
        episode = asyncio.run(self._fetch(Episode, ext_id="ep_reg"))
        self.assertEqual(episode.status, "pending")
        job = asyncio.run(self._fetch(Job, kind="script", ref_id="ep_reg"))
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "pending")

    def test_regenerate_not_found(self) -> None:
        resp = self.client.post("/api/episodes/9999/regenerate", json={"reason": "x"})
        self.assertEqual(resp.status_code, 404)

    def test_script_returns_review_and_errata(self) -> None:
        script_file = json.loads(settings.script_path("ep_reg").read_text("utf-8"))
        script_file["review"] = {"verdict": "pass", "flags": [{"type": "soft"}],
                                 "reviewer_model": "fake", "reviewed_at": "2026-09-01"}
        self._write_file(settings.script_path("ep_reg"), script_file)
        artifacts.append_errata("ep_reg", "数字有误")

        resp = self.client.get(f"/api/episodes/{self.episode_db_id}/script")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["review"]["verdict"], "pass")
        self.assertEqual(len(payload["errata"]), 1)

    def test_qc_writes_human_qc_to_meta(self) -> None:
        resp = self.client.post(f"/api/episodes/{self.episode_db_id}/qc",
                                json={"passed": True, "notes": "真听通过"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "passed")
        qc = artifacts.load_human_qc("ep_reg")
        self.assertEqual(qc["status"], "passed")
        self.assertEqual(qc["notes"], "真听通过")

    def test_qc_not_found(self) -> None:
        resp = self.client.post("/api/episodes/9999/qc", json={"passed": True})
        self.assertEqual(resp.status_code, 404)


# ---------- worker 全链路:block → 重试 → failed ----------

class WorkerBlockedChainTest(unittest.TestCase):
    def setUp(self) -> None:
        asyncio.run(db.close_db())
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp.name)
        self.old_reviewer = settings.reviewer_enabled
        settings.reviewer_enabled = True
        # 参考猎手会真调 LLM/网络;本组只测 block→重试→failed 链路,关掉
        self.old_ref_hunter = settings.ref_hunter_enabled
        settings.ref_hunter_enabled = False
        asyncio.run(db.init_db())

        plan_rec = PlanRec(plan_id="plan_chain", title="分布式锁", direction="backend_arch",
                           user_topic="分布式锁",
                           episodes=[EpisodeRec(episode_id="ep_chain", seq=1, title="Redlock 争议",
                                                goal="g", objectives=["redlock"], outline="o")])
        planner_service.save_plan(plan_rec)
        asyncio.run(self._seed())
        reviewer_service._set_provider(FakeProvider(_review_raw("hard")))

    def tearDown(self) -> None:
        settings.data_dir = self.old_data_dir
        settings.reviewer_enabled = self.old_reviewer
        settings.ref_hunter_enabled = self.old_ref_hunter
        reviewer_service._set_provider(None)
        asyncio.run(db.close_db())
        self.tmp.cleanup()

    async def _seed(self) -> None:
        async with db.session() as s:
            topic = Topic(title="链路", direction="backend_arch")
            s.add(topic)
            await s.flush()
            plan = Plan(ext_id="plan_chain", topic_id=topic.id, title="链路计划",
                        status="active", episode_count=1)
            s.add(plan)
            await s.flush()
            s.add(Episode(ext_id="ep_chain", plan_id=plan.id, seq=1,
                          title="Redlock 争议", status="scripted"))
            await s.commit()

    async def _status(self, model, pk: int) -> str:
        async with db.session() as s:
            row = await s.get(model, pk)
            return row.status

    async def _episode_status(self) -> str:
        from sqlalchemy import select
        async with db.session() as s:
            row = await s.scalar(select(Episode).where(Episode.ext_id == "ep_chain"))
            return row.status

    def test_block_retries_then_fails_episode(self) -> None:
        from drivecast.jobs import pipeline, worker

        with patch("drivecast.scriptwriter.service.get_provider",
                   return_value=FakeProvider(_fake_script_out())), \
             patch("drivecast.scriptwriter.service.upsert_script", new=AsyncMock()):
            job_id = asyncio.run(pipeline.enqueue_episode("plan_chain", 1, "ep_chain"))
            for attempt in range(1, 4):
                job = asyncio.run(worker._next_pending())
                self.assertIsNotNone(job, f"第 {attempt} 次应能领到 job")
                asyncio.run(worker._execute_one(job))
                status = asyncio.run(self._status(Job, job_id))
                # attempt<max_attempt 回 pending 重试;打满 3 次才 failed(docs/07 §6.3)
                self.assertEqual(status, "pending" if attempt < 3 else "failed")

        self.assertFalse(settings.script_path("ep_chain").exists())  # 全程被拦截,未落盘
        self.assertEqual(asyncio.run(self._episode_status()), "failed")


if __name__ == "__main__":
    unittest.main()
