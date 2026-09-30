"""scripts/backup.py:在线快照可恢复,且包含运行必需的 JSON、排除可重建中间片。"""
from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from drivecast import db
from drivecast.config import settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("backup_script", PROJECT_ROOT / "scripts" / "backup.py")
backup_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_script)


class BackupScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.enterContext(patch.object(settings, "data_dir", root / "data"))
        asyncio.run(db.init_db())
        self.addCleanup(lambda: asyncio.run(db.close_db()))
        for rel, body in {
            "plans/plan_x.json": "{}",
            "scripts/ep_x.script.json": "{}",
            "tts/ep_x.tts.json": "{}",
            "meta/ep_x.meta.json": "{}",
            "audio/episodes/ep_x.mp3": "ID3",
            "audio/chunks/ep_x/001.mp3": "ID3",
            "audio/episodes/ep_x.part.mp3": "ID3",
        }.items():
            path = settings.data_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, "utf-8")
        self.out = root / "out"

    def _names(self, target: Path) -> set[str]:
        with tarfile.open(target) as tar:
            return set(tar.getnames())

    def test_backup_contains_db_snapshot_and_runtime_files(self) -> None:
        target = backup_script.backup(self.out, keep=0, with_chunks=False)
        names = self._names(target)
        for rel in ("drivecast.db", "plans/plan_x.json", "scripts/ep_x.script.json",
                    "tts/ep_x.tts.json", "meta/ep_x.meta.json", "audio/episodes/ep_x.mp3"):
            self.assertIn(f"data/{rel}", names)
        self.assertNotIn("data/audio/chunks/ep_x/001.mp3", names)
        self.assertNotIn("data/audio/episodes/ep_x.part.mp3", names)

        restore = Path(self.tmp.name) / "restore"
        with tarfile.open(target) as tar:
            tar.extractall(restore, filter="data")
        conn = sqlite3.connect(restore / "data" / "drivecast.db")
        try:
            self.assertEqual(conn.execute("pragma integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("select username from user where id=1").fetchone()[0], "admin")
        finally:
            conn.close()

    def test_with_chunks_and_retention(self) -> None:
        self.out.mkdir(parents=True)
        for i in range(3):
            (self.out / f"drivecast-2026010{i}-000000.tgz").write_bytes(b"old")
        target = backup_script.backup(self.out, keep=2, with_chunks=True)
        self.assertIn("data/audio/chunks/ep_x/001.mp3", self._names(target))
        kept = sorted(p.name for p in self.out.glob("drivecast-*.tgz"))
        self.assertEqual(len(kept), 2)
        self.assertIn(target.name, kept)


if __name__ == "__main__":
    unittest.main()
