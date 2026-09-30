"""drivecast CLI(docs/05 M1-5 + M2-3)。

  python -m drivecast plan "分布式锁与 Redlock 争议" --direction backend_arch --episodes 6
  python -m drivecast episode <plan_id> <seq>    # 单集:脚本+音频一把出
  python -m drivecast generate <plan_id>         # 整计划批量(job 队列 + worker,断点续跑)
  python -m drivecast worker                     # 消费 job 队列(中断恢复)
  python -m drivecast list <plan_id>             # 看计划与状态
  python -m drivecast dbsync                     # 存量文件产物导入 DB
  python -m drivecast create-user <username>     # 建登录用户(无注册接口)
  python -m drivecast reset-password <username>  # 重置密码(0006 后给 admin 后置密码)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from sqlalchemy import select

from .config import ensure_data_dirs, settings
from .db import session
from .library import service as library
from .models import Episode, Plan, User
from .planner import service as planner
from .schemas import Direction
from .scriptwriter import service as scriptwriter
from .tts import service as tts

DIRECTIONS = ["backend_arch", "ai_llm", "tech_news", "source_dive"]


def _get_ep(plan, seq: int):
    ep = next((e for e in plan.episodes if e.seq == seq), None)
    if ep is None:
        sys.exit(f"计划 {plan.plan_id} 里没有第 {seq} 集")
    return ep


async def cmd_plan(args) -> None:
    plan = await planner.create_plan(
        args.topic, direction=args.direction,
        episode_count=args.episodes, target_minutes=args.minutes,
    )
    print(f"\n计划已生成: {plan.plan_id} 「{plan.title}」\n")
    for ep in plan.episodes:
        print(f"  {ep.seq}. {ep.title}")
        print(f"     目标: {ep.goal}")
    print(f"\n下一步: python -m drivecast episode {plan.plan_id} 1")


async def cmd_script(args) -> None:
    plan = planner.load_plan(args.plan_id)
    ep = _get_ep(plan, args.seq)
    data = await scriptwriter.write_script(plan, ep, force=args.force)
    print(f"脚本就绪: {data['episode_id']} ({data['char_count']} 字 / 目标 {data['target_words']})")
    print(f"自测题: {len(data['questions'])} 道")


async def cmd_tts(args) -> None:
    plan = planner.load_plan(args.plan_id)
    ep = _get_ep(plan, args.seq)
    script = scriptwriter.load_script(ep.episode_id)
    if not script:
        sys.exit(f"{ep.episode_id} 还没有脚本,先跑: python -m drivecast script {args.plan_id} {args.seq}")
    meta = await tts.synthesize_episode(script, album=plan.title)
    print(f"音频就绪: {meta['audio_path']}")


async def cmd_episode(args) -> None:
    plan = planner.load_plan(args.plan_id)
    ep = _get_ep(plan, args.seq)
    print(f"[1/2] 生成脚本: {ep.title}")
    script = await scriptwriter.write_script(plan, ep, force=args.force)
    print(f"      {script['char_count']} 字(目标 {script['target_words']})")
    print(f"[2/2] 合成音频")
    meta = await tts.synthesize_episode(script, album=plan.title)
    print(f"\n完成: {meta['audio_path']}  ({meta['duration_sec'] / 60:.1f} 分钟)")
    print("自测题:")
    for q in script["questions"]:
        print(f"  [{q['qtype']}] {q['question']}")


async def cmd_generate(args) -> None:
    """整计划批量生成(docs/05 M2-3):入队 script job(完成自动链 tts),worker 串行消费。"""
    from .jobs import pipeline, worker
    plan = planner.load_plan(args.plan_id)
    job_ids = await pipeline.enqueue_plan_all(plan)
    if not job_ids:
        print("所有集都已有音频,无需生成")
        return
    print(f"已入队 {len(job_ids)} 个 script job(每个完成自动链 tts job)")
    await worker.run_until_empty()


async def cmd_worker(args) -> None:
    from .jobs import worker
    await worker.run_until_empty()


async def cmd_dbsync(args) -> None:
    """存量文件产物 → DB(plans/scripts/meta 全部双写补录)。"""
    import json

    from .config import settings
    from .persist import upsert_audio, upsert_plan, upsert_script
    from .schemas import PlanRec

    count = 0
    for plan_file in sorted((settings.data_dir / "plans").glob("plan_*.json")):
        plan = PlanRec.model_validate(json.loads(plan_file.read_text("utf-8")))
        await upsert_plan(plan)
        for ep in plan.episodes:
            script = scriptwriter.load_script(ep.episode_id)
            if script:
                await upsert_script(ep.episode_id, script)
            meta_file = settings.meta_path(ep.episode_id)
            audio_file = settings.episode_audio_path(ep.episode_id)
            if meta_file.exists() and audio_file.exists():
                meta = json.loads(meta_file.read_text("utf-8"))
                await upsert_audio(ep.episode_id, meta, voice="")
        print(f"  ✓ {plan.plan_id} 「{plan.title}」({len(plan.episodes)} 集)")
        count += 1
    print(f"共导入 {count} 个计划")


def _available_plans_text() -> str:
    """列出 data/plans/*.json 里的 plan_id 与标题,用于友好报错引导。"""
    lines = []
    for plan_file in sorted((settings.data_dir / "plans").glob("plan_*.json")):
        try:
            data = json.loads(plan_file.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        lines.append(f"  {data.get('plan_id', plan_file.stem)} 「{data.get('title', '')}」")
    return "\n".join(lines) if lines else "  (plans 目录下还没有计划)"


async def _resolve_plan_ext_id(raw: str) -> str:
    """把 CLI 入参解析成文件态 plan_id:优先已有文件;纯数字按 DB 自增 id 查 ext_id。"""
    if settings.plan_path(raw).exists():
        return raw
    if raw.isdigit():
        plan = await library.get_plan_by_id(int(raw))
        if plan is not None and settings.plan_path(plan.ext_id).exists():
            return plan.ext_id
    sys.exit(f"找不到计划 '{raw}',现有计划:\n{_available_plans_text()}")


async def _db_episode_statuses(plan_ext_id: str) -> dict[str, str]:
    """查 DB 里该计划各集的真实状态(文件态会滞后);查不到时由调用方回退文件态。"""
    async with session() as s:
        result = await s.execute(
            select(Episode.ext_id, Episode.status)
            .join(Plan, Plan.id == Episode.plan_id)
            .where(Plan.ext_id == plan_ext_id)
        )
        return dict(result.all())


async def cmd_list(args) -> None:
    plan_ext_id = await _resolve_plan_ext_id(args.plan_id)
    plan = planner.load_plan(plan_ext_id)
    db_statuses = await _db_episode_statuses(plan_ext_id)
    print(f"{plan.plan_id} 「{plan.title}」 方向={plan.direction} 创建于 {plan.created_at}")
    for ep in plan.episodes:
        status = db_statuses.get(ep.episode_id, ep.status)  # DB 优先,回退文件态
        has_script = scriptwriter.load_script(ep.episode_id) is not None
        print(f"  {ep.seq}. [{status}] {ep.title}  脚本={'✓' if has_script else '✗'}")


def _read_password(username: str, password_arg: str | None) -> str:
    """密码来源优先级: --password > 环境变量 DRIVECAST_USER_PASSWORD > 交互输入(无 tty 报错)。"""
    if password_arg:
        return password_arg
    env_pw = os.environ.get("DRIVECAST_USER_PASSWORD")
    if env_pw:
        return env_pw
    if not sys.stdin.isatty():
        sys.exit(
            "未提供密码:请用 --password <pw> 或环境变量 DRIVECAST_USER_PASSWORD"
            "(当前非交互环境,无法交互输入)"
        )
    import getpass

    pw = getpass.getpass(f"为用户 {username} 设置密码: ")
    if not pw:
        sys.exit("密码不能为空")
    return pw


async def cmd_create_user(args) -> None:
    """建登录用户(系统不提供注册 API,用户只能从这里来)。"""
    from . import auth
    from .db import init_db

    await init_db()  # 空库自动建表;旧 schema 会得到"先跑 alembic"的可操作报错
    password = _read_password(args.username, args.password)
    async with session() as s:
        exists = (
            await s.execute(select(User).where(User.username == args.username))
        ).scalar_one_or_none()
        if exists is not None:
            sys.exit(f"用户 '{args.username}' 已存在(id={exists.id});如需改密码请用 reset-password")
        user = User(username=args.username, password_hash=auth.hash_password(password))
        s.add(user)
        await s.commit()
        print(f"用户已创建: id={user.id} username={user.username}")


async def cmd_reset_password(args) -> None:
    """重置密码并自增 token_version:该用户所有已签发 token 立即失效。"""
    from . import auth
    from .db import init_db

    await init_db()
    password = _read_password(args.username, args.password)
    async with session() as s:
        user = (
            await s.execute(select(User).where(User.username == args.username))
        ).scalar_one_or_none()
        if user is None:
            sys.exit(f"用户 '{args.username}' 不存在;可用 create-user 创建")
        user.password_hash = auth.hash_password(password)
        user.token_version = User.token_version + 1
        await s.commit()
        print(f"密码已重置: id={user.id} username={user.username}")
        print("该用户已签发的全部 token 已失效,各端需重新登录。")


def main() -> None:
    ensure_data_dirs()
    p = argparse.ArgumentParser(prog="drivecast", description="通勤技术播客生成器")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="生成学习计划")
    sp.add_argument("topic")
    sp.add_argument("--direction", choices=DIRECTIONS, default="backend_arch")
    sp.add_argument("--episodes", type=int, default=None)
    sp.add_argument("--minutes", type=int, default=None)

    for name, fn, helptext in [
        ("script", cmd_script, "生成单集脚本"),
        ("tts", cmd_tts, "合成单集音频"),
        ("episode", cmd_episode, "脚本+音频一把出"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("plan_id")
        sp.add_argument("seq", type=int)
        sp.add_argument("--force", action="store_true", help="忽略已有产物重新生成")

    sp = sub.add_parser("generate", help="整计划批量生成(job+worker)")
    sp.add_argument("plan_id")

    sub.add_parser("worker", help="消费 job 队列(中断恢复)")
    sub.add_parser("dbsync", help="存量文件产物导入 DB")

    for name, helptext in [
        ("create-user", "创建登录用户(密码 --password 或环境变量 DRIVECAST_USER_PASSWORD)"),
        ("reset-password", "重置用户密码"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("username")
        sp.add_argument("--password", default=None, help="明文密码;缺省读环境变量或交互输入")

    sp = sub.add_parser("list", help="查看计划")
    sp.add_argument("plan_id")

    args = p.parse_args()
    commands = {
        "plan": cmd_plan, "script": cmd_script, "tts": cmd_tts, "episode": cmd_episode,
        "generate": cmd_generate, "worker": cmd_worker, "dbsync": cmd_dbsync,
        "list": cmd_list, "create-user": cmd_create_user, "reset-password": cmd_reset_password,
    }

    async def run_command() -> None:
        from .db import close_db, init_db

        try:
            await init_db()  # 任一 CLI 写入之前均先完成迁移并保留 admin
            await commands[args.cmd](args)
        finally:
            await close_db()

    asyncio.run(run_command())


if __name__ == "__main__":
    main()
