from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivecast import db
from drivecast.config import settings
from drivecast.library import service
from drivecast.models import Audio, Base, Episode, Plan, Question, ReviewReplay, Topic


class ReviewServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.data_dir
        self.old_now = service._now
        settings.data_dir = Path(self.tmp.name)
        service._now = lambda: datetime(2026, 8, 20, 9, 0, 0)
        await db.init_db()

        async with db.session() as s:
            backend = Topic(title="分布式锁", direction="backend_arch")
            ai = Topic(title="RAG", direction="ai_llm")
            s.add_all([backend, ai])
            await s.flush()
            backend_plan = Plan(
                ext_id="plan_backend", topic_id=backend.id, title="锁",
                status="active", episode_count=2,
            )
            ai_plan = Plan(
                ext_id="plan_ai", topic_id=ai.id, title="RAG",
                status="active", episode_count=1,
            )
            s.add_all([backend_plan, ai_plan])
            await s.flush()
            long_ep = Episode(
                ext_id="ep_long", plan_id=backend_plan.id, seq=1,
                title="长集", status="ready",
            )
            short_ep = Episode(
                ext_id="ep_short", plan_id=backend_plan.id, seq=2,
                title="短集", status="ready",
            )
            ai_ep = Episode(
                ext_id="ep_ai", plan_id=ai_plan.id, seq=1,
                title="RAG 集", status="ready",
            )
            s.add_all([long_ep, short_ep, ai_ep])
            await s.flush()
            s.add_all([
                Audio(episode_id=long_ep.id, file_path="long.mp3", duration_sec=900),
                Audio(episode_id=short_ep.id, file_path="short.mp3", duration_sec=500),
                Audio(episode_id=ai_ep.id, file_path="ai.mp3", duration_sec=600),
            ])
            s.add_all([
                Question(episode_id=long_ep.id, kp_id="kp_lock", question="q1", framework="[]"),
                Question(episode_id=short_ep.id, kp_id="kp_lock", question="q2", framework="[]"),
                Question(episode_id=short_ep.id, kp_id="", question="q3", framework="[]"),
                Question(episode_id=short_ep.id, kp_id="", question="q4", framework="[]"),
                Question(episode_id=ai_ep.id, kp_id="kp_rag", question="q5", framework="[]"),
                Question(episode_id=ai_ep.id, kp_id="kp_rag", question="q6", framework="[]"),
            ])
            await s.commit()

    async def asyncTearDown(self) -> None:
        await db.close_db()
        settings.data_dir = self.old_data_dir
        service._now = self.old_now
        self.tmp.cleanup()

    async def test_grade_records_transition_count_and_due_time(self) -> None:
        fuzzy = await service.grade_question(1, "fuzzy")
        self.assertEqual(fuzzy["review_count"], 1)
        self.assertEqual(fuzzy["last_reviewed_at"], "2026-08-20 09:00:00")
        self.assertEqual(fuzzy["next_review_at"], "2026-08-22 09:00:00")

        mastered = await service.grade_question(1, "mastered")
        self.assertEqual(mastered["review_count"], 2)
        self.assertEqual(mastered["next_review_at"], "2026-08-27 09:00:00")

        unknown = await service.grade_question(1, "unknown")
        self.assertEqual(unknown["review_count"], 3)
        self.assertEqual(unknown["next_review_at"], "2026-08-21 09:00:00")

    async def test_due_questions_put_unscheduled_first(self) -> None:
        await service.grade_question(1, "unknown")
        rows = await service.today_review()
        self.assertTrue(rows)
        self.assertIsNone(rows[0]["next_review_at"])
        self.assertIn("last_reviewed_at", rows[0])
        self.assertIsInstance(rows[0]["plan_id"], int)

    async def test_blank_kp_groups_and_replay_chooses_shortest_episode(self) -> None:
        weak = await service.weak_points()
        summary = {
            row["kp_id"]: (
                row["weak_question_count"],
                row["recommended_episode"]["episode_id"],
            )
            for row in weak
        }
        self.assertEqual(summary["episode:2"], (2, 2))
        self.assertEqual(summary["kp_lock"], (2, 2))
        self.assertTrue(all(row["recommended_episode"]["plan_id"] == 1 for row in weak if row["direction"] == "backend_arch"))

        queue = await service.sync_replay_queue()
        self.assertEqual(len(queue), 3)
        self.assertTrue(all(row["scheduled_for"] == "2026-08-21" for row in queue))
        self.assertTrue(all(isinstance(row["plan_id"], int) for row in queue))

        await service.grade_question(1, "mastered")
        queue = await service.replay_queue(date(2026, 8, 21))
        self.assertNotIn("kp_lock", {row["kp_id"] for row in queue})

    async def test_default_replay_queue_keeps_due_day_visible(self) -> None:
        await service.sync_replay_queue()
        service._now = lambda: datetime(2026, 8, 21, 9, 0, 0)

        due = await service.replay_queue()
        self.assertEqual(len(due), 3)
        self.assertTrue(all(row["scheduled_for"] == "2026-08-21" for row in due))

        refreshed = await service.sync_replay_queue()
        self.assertEqual({row["id"] for row in refreshed}, {row["id"] for row in due})
        self.assertTrue(all(row["scheduled_for"] == "2026-08-21" for row in refreshed))

    async def test_interview_date_and_sprint_grouping(self) -> None:
        await service.set_interview_date(date(2026, 8, 25))
        sprint = await service.sprint_list()
        self.assertEqual(sprint["sprint_start"], "2026-08-22")
        self.assertEqual(sprint["sprint_end"], "2026-08-24")
        self.assertEqual(sprint["directions"][0]["direction"], "backend_arch")
        for direction in sprint["directions"]:
            counts = [row["weak_question_count"] for row in direction["episodes"]]
            self.assertEqual(counts, sorted(counts, reverse=True))
            self.assertTrue(all(isinstance(row["plan_id"], int) for row in direction["episodes"]))

        await service.set_interview_date(None)
        self.assertIsNone(await service.get_interview_date())


class ReviewMigrationTest(unittest.TestCase):
    def _config(self, tmp: Path, db_path: Path) -> Path:
        config_path = tmp / "alembic.ini"
        source = (PROJECT_ROOT / "alembic.ini").read_text("utf-8")
        config_path.write_text(
            source.replace(
                "script_location = alembic",
                f"script_location = {PROJECT_ROOT / 'alembic'}",
            ).replace(
                "sqlite:///data/drivecast.db",
                f"sqlite:///{db_path}",
            ),
            "utf-8",
        )
        return config_path

    def _upgrade(self, config_path: Path, revision: str) -> None:
        subprocess.run(
            [str(PROJECT_ROOT / ".venv/bin/alembic"), "-c", str(config_path), "upgrade", revision],
            check=True, cwd=PROJECT_ROOT, capture_output=True, text=True,
        )

    def test_review_migration_is_in_chain_to_sole_0004_head(self) -> None:
        alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
        script = ScriptDirectory.from_config(alembic_config)
        self.assertEqual(script.get_heads(), ["0004"])
        revisions = {revision.revision for revision in script.walk_revisions()}
        self.assertIn("0002", revisions)

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            db_path = tmp / "drivecast.db"
            config_path = tmp / "alembic.ini"
            source = (PROJECT_ROOT / "alembic.ini").read_text("utf-8")
            config_path.write_text(
                source.replace(
                    "script_location = alembic",
                    f"script_location = {PROJECT_ROOT / 'alembic'}",
                ).replace(
                    "sqlite:///data/drivecast.db",
                    f"sqlite:///{db_path}",
                ),
                "utf-8",
            )
            env = os.environ.copy()
            subprocess.run(
                [str(PROJECT_ROOT / ".venv/bin/alembic"), "-c", str(config_path), "upgrade", "0001"],
                check=True, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
            )
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.executescript("""
                    INSERT INTO topic(title, direction) VALUES ('主题', 'backend_arch');
                    INSERT INTO plan(ext_id, topic_id, title) VALUES ('p', 1, '计划');
                    INSERT INTO episode(ext_id, plan_id, seq, title) VALUES ('e', 1, 1, '单集');
                    INSERT INTO question(episode_id, question) VALUES (1, '历史题');
                """)
            subprocess.run(
                [str(PROJECT_ROOT / ".venv/bin/alembic"), "-c", str(config_path), "upgrade", "head"],
                check=True, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
            )
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                question_columns = {row[1] for row in conn.execute("PRAGMA table_info(question)")}
                audio_columns = {row[1] for row in conn.execute("PRAGMA table_info(audio)")}
                kp_id = conn.execute("SELECT kp_id FROM question").fetchone()[0]
                version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                settings_row = conn.execute("SELECT id FROM review_settings").fetchone()
            self.assertIn("last_reviewed_at", question_columns)
            self.assertTrue({"meta_json", "quality_status"}.issubset(audio_columns))
            self.assertEqual(kp_id, "episode:1")
            self.assertEqual(version, "0004")
            self.assertEqual(settings_row, (1,))

    def test_0004_repairs_create_all_tables_defaults_and_backfills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            db_path = tmp / "drivecast.db"
            config_path = self._config(tmp, db_path)
            self._upgrade(config_path, "0001")

            # 精确复现：业务启动在 Alembic 0002 前先执行 ORM create_all。
            from sqlalchemy import create_engine

            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            engine.dispose()
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.executescript("""
                    INSERT INTO topic(title, direction) VALUES ('主题', 'backend_arch');
                    INSERT INTO plan(ext_id, topic_id, title) VALUES ('p', 1, '计划');
                    INSERT INTO episode(ext_id, plan_id, seq, title, status)
                    VALUES ('e', 1, 1, '单集', 'ready');
                    INSERT INTO question(
                      episode_id, kp_id, question, review_count, next_review_at
                    ) VALUES (1, 'kp', '历史题', 2, '2026-08-21 09:00:00');
                    INSERT INTO review_replay(
                      kp_id, episode_id, scheduled_for, weak_question_count, status
                    ) VALUES ('kp', 1, '2026-08-21', 2, '');
                """)

            self._upgrade(config_path, "head")
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.execute("""
                    INSERT INTO review_replay(
                      kp_id, episode_id, scheduled_for, weak_question_count
                    ) VALUES ('kp2', 1, '2026-08-22', 2)
                """)
                replay = conn.execute("""
                    SELECT status, created_at FROM review_replay
                    WHERE kp_id = 'kp2'
                """).fetchone()
                repaired = conn.execute("""
                    SELECT status, created_at FROM review_replay
                    WHERE kp_id = 'kp'
                """).fetchone()
                last_reviewed_at = conn.execute(
                    "SELECT last_reviewed_at FROM question WHERE id = 1"
                ).fetchone()[0]
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO review_settings(id) VALUES (2)")

            self.assertEqual(replay[0], "pending")
            self.assertNotIn("datetime(", replay[1])
            self.assertEqual(repaired[0], "pending")
            self.assertNotIn("datetime(", repaired[1])
            self.assertEqual(last_reviewed_at, "2026-08-21 09:00:00")

    def test_0003_tolerates_columns_created_before_alembic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            db_path = tmp / "drivecast.db"
            config_path = tmp / "alembic.ini"
            source = (PROJECT_ROOT / "alembic.ini").read_text("utf-8")
            config_path.write_text(
                source.replace(
                    "script_location = alembic",
                    f"script_location = {PROJECT_ROOT / 'alembic'}",
                ).replace(
                    "sqlite:///data/drivecast.db",
                    f"sqlite:///{db_path}",
                ),
                "utf-8",
            )
            subprocess.run(
                [str(PROJECT_ROOT / ".venv/bin/alembic"), "-c", str(config_path), "upgrade", "0002"],
                check=True, cwd=PROJECT_ROOT, capture_output=True, text=True,
            )
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                conn.execute("ALTER TABLE audio ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
                conn.execute("ALTER TABLE audio ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'unknown'")
            subprocess.run(
                [str(PROJECT_ROOT / ".venv/bin/alembic"), "-c", str(config_path), "upgrade", "head"],
                check=True, cwd=PROJECT_ROOT, capture_output=True, text=True,
            )
            with contextlib.closing(sqlite3.connect(db_path)) as conn:
                version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                columns = [row[1] for row in conn.execute("PRAGMA table_info(audio)")]
            self.assertEqual(version, "0004")
            self.assertEqual(columns.count("meta_json"), 1)
            self.assertEqual(columns.count("quality_status"), 1)


if __name__ == "__main__":
    unittest.main()
