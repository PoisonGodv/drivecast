"""一键备份 data/:SQLite 在线一致性快照 + 运行必需的全部文件,打成单个 tgz。

用法:
  .venv/bin/python scripts/backup.py [--out /backup] [--keep 14] [--with-chunks]

- 数据库用 sqlite3 backup API 做在线快照(含 WAL 未合并页),服务不停也安全;
- plans/scripts/tts/meta/audio/episodes 全部打包(load_plan 等运行时直接读这些 JSON);
- 默认排除 audio/chunks(TTS 中间片,可由 tts.json 重新合成,体积大);
- .env 含密钥,不进备份包,请单独存放到秘密管理器;
- --keep N 只保留最近 N 份 drivecast-*.tgz。

恢复:停服务 → 解包覆盖到项目目录 → 启动(启动时自动迁移到 head)。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivecast.config import settings  # noqa: E402

_SKIP_TOP = {"audio/chunks"}
_SKIP_SUFFIXES = (".db", ".db-wal", ".db-shm", ".part.mp3", ".joined.mp3", ".tmp")


def _snapshot_db(db_path: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _include(rel: Path, with_chunks: bool) -> bool:
    text = rel.as_posix()
    if not with_chunks and any(text == p or text.startswith(p + "/") for p in _SKIP_TOP):
        return False
    return not text.endswith(_SKIP_SUFFIXES) and ".db.bak" not in text


def backup(out_dir: Path, *, keep: int, with_chunks: bool) -> Path:
    data_dir = settings.data_dir.resolve()
    db_path = data_dir / "drivecast.db"
    if not db_path.exists():
        raise SystemExit(f"找不到数据库 {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"drivecast-{stamp}.tgz"
    partial = target.with_suffix(".tgz.part")

    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "drivecast.db"
        _snapshot_db(db_path, snap)
        with tarfile.open(partial, "w:gz") as tar:
            tar.add(snap, arcname="data/drivecast.db")
            for path in sorted(data_dir.rglob("*")):
                rel = path.relative_to(data_dir)
                if path.is_file() and _include(rel, with_chunks):
                    tar.add(path, arcname=f"data/{rel.as_posix()}")
    partial.replace(target)

    if keep > 0:
        for old in sorted(out_dir.glob("drivecast-*.tgz"))[:-keep]:
            old.unlink()
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="备份 drivecast 的 data/ 目录")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "backups")
    parser.add_argument("--keep", type=int, default=14, help="保留最近 N 份,0 表示不清理")
    parser.add_argument("--with-chunks", action="store_true", help="连 audio/chunks 中间片一起备份")
    args = parser.parse_args()
    target = backup(args.out, keep=args.keep, with_chunks=args.with_chunks)
    print(f"备份完成: {target} ({target.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
