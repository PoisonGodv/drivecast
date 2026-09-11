"""存量数据排期回填(0005 配套,一次性)。

背景:0005 之前生成的计划,episode.scheduled_date 为 NULL、question.next_review_at
为 NULL,今日复习会被全部挤爆。按新规则回填:
- 已听/在听的集:scheduled_date = 今天(立刻进入复习)。
- 未听的集:按计划内 seq 顺序从今天开始逐日排开(每日 1 集新学,与 create_plan 语义一致)。
- 从未复习的题:next_review_at = 所属集排期日 09:00;已过 09:00 取当前时间。

用法:.venv/bin/python scripts/backfill_schedule.py
幂等:只处理 scheduled_date / next_review_at 仍为 NULL 的行。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "drivecast.db"


def main() -> None:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    today = date.today()
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    episodes = db.execute(
        "SELECT id, plan_id, seq, listen_status FROM episode "
        "WHERE scheduled_date IS NULL ORDER BY plan_id, seq"
    ).fetchall()

    ep_dates: dict[int, str] = {}
    # 每个计划独立排:第一个未听集今天,其后逐日 +1
    offsets: dict[int, int] = {}
    for ep in episodes:
        if ep["listen_status"] in ("done", "listening"):
            ep_dates[ep["id"]] = today.isoformat()
            continue
        offset = offsets.get(ep["plan_id"], 0)
        ep_dates[ep["id"]] = (today + timedelta(days=offset)).isoformat()
        offsets[ep["plan_id"]] = offset + 1

    for ep_id, day in ep_dates.items():
        db.execute("UPDATE episode SET scheduled_date = ? WHERE id = ?", (day, ep_id))

    questions = db.execute(
        "SELECT id, episode_id FROM question WHERE next_review_at IS NULL"
    ).fetchall()
    for q in questions:
        day = ep_dates.get(q["episode_id"], today.isoformat())
        due = f"{day} 09:00:00"
        db.execute(
            "UPDATE question SET next_review_at = ? WHERE id = ?",
            (max(due, now_text), q["id"]),
        )

    db.commit()
    print(f"episodes backfilled: {len(ep_dates)}, questions backfilled: {len(questions)}", file=sys.stderr)


if __name__ == "__main__":
    main()
