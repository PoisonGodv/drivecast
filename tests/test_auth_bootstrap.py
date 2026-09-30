"""认证初始化回归：所有数据库均位于 tmp_path，不使用真实 data/。"""
import asyncio
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock
from functools import wraps
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from drivecast import auth
from drivecast.__main__ import cmd_create_user, cmd_reset_password
from drivecast.config import settings
from drivecast.db import close_db, get_engine, init_db, session
from drivecast.models import Base


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


@pytest.fixture
def bootstrap_db(tmp_path, monkeypatch):
    asyncio.run(close_db())
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    yield tmp_path / "drivecast.db"
    asyncio.run(close_db())


def migrate(path: Path, revision="head"):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, revision)


async def rows(sql):
    async with session() as s:
        return (await s.execute(text(sql))).all()


@async_test
async def test_create_user_first_reserves_locked_admin_and_unlocks(bootstrap_db):
    await cmd_create_user(Namespace(username="alice", password="alice-password-123"))
    users = await rows("SELECT id, username, password_hash FROM user ORDER BY id")
    assert [(u.id, u.username) for u in users] == [(1, "admin"), (2, "alice")]
    assert users[0].password_hash == "!locked"
    assert not auth.verify_password("anything", users[0].password_hash)
    await cmd_reset_password(Namespace(username="admin", password="admin-password-123"))
    before = await rows("SELECT id, username, password_hash FROM user ORDER BY id")
    assert auth.verify_password("admin-password-123", before[0].password_hash)
    await close_db()
    await init_db()
    await init_db()
    assert await rows("SELECT id, username, password_hash FROM user ORDER BY id") == before
    assert await rows("SELECT version_num FROM alembic_version") == [("0008",)]


@async_test
@pytest.mark.parametrize("unversioned", [False, True])
async def test_legacy_0005_ownership_and_restart(bootstrap_db, unversioned):
    migrate(bootstrap_db, "0005")
    engine = create_engine(f"sqlite:///{bootstrap_db}")
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO topic(title,direction) VALUES ('legacy','backend_arch')")
        c.exec_driver_sql("INSERT INTO plan(ext_id,topic_id,title) VALUES ('legacy',1,'legacy')")
        c.exec_driver_sql("INSERT INTO episode(ext_id,plan_id,seq,title) VALUES ('legacy_ep',1,1,'legacy')")
        c.exec_driver_sql("INSERT INTO job(kind,ref_id) VALUES ('script','legacy_ep')")
        c.exec_driver_sql("INSERT INTO review_replay(id,kp_id,episode_id,scheduled_for,weak_question_count) VALUES (42,'kp',1,'2026-10-01',1)")
        c.exec_driver_sql("UPDATE review_settings SET interview_date='2026-10-20' WHERE id=1")
        if unversioned:
            c.exec_driver_sql("DROP TABLE alembic_version")
    engine.dispose()
    await init_db()
    for table in ("topic", "plan", "job", "review_replay", "review_settings"):
        assert await rows(f"SELECT user_id FROM {table}") == [(1,)]
    assert await rows("SELECT id FROM review_replay") == [(42,)]
    assert await rows("SELECT interview_date FROM review_settings") == [("2026-10-20",)]
    await close_db()
    await init_db()
    assert await rows("SELECT id,user_id FROM review_replay") == [(42, 1)]
    assert await rows("SELECT interview_date FROM review_settings") == [("2026-10-20",)]


@async_test
async def test_unversioned_create_all_preserves_existing_tenants(bootstrap_db):
    async with get_engine().begin() as c:
        await c.run_sync(Base.metadata.create_all)
        await c.exec_driver_sql("INSERT INTO user(id,username,password_hash) VALUES (1,'admin','!locked'),(2,'alice','existing-hash')")
        await c.exec_driver_sql("INSERT INTO review_settings(user_id,interview_date) VALUES (1,'2026-10-01'),(2,'2026-11-01')")
    await close_db()
    migrate(bootstrap_db)  # 与 init_db 相同的直接 Alembic 入口
    await init_db()
    assert await rows("SELECT user_id,interview_date FROM review_settings ORDER BY user_id") == [(1, "2026-10-01"), (2, "2026-11-01")]
    assert await rows("SELECT password_hash FROM user WHERE id=2") == [("existing-hash",)]


@async_test
@pytest.mark.parametrize("versioned", [False, True])
async def test_conflicting_first_user_fails_without_reassigning_data(bootstrap_db, versioned):
    async with get_engine().begin() as c:
        await c.run_sync(Base.metadata.create_all)
        await c.exec_driver_sql("INSERT INTO user(id,username,password_hash) VALUES (1,'alice','untouched')")
        await c.exec_driver_sql("INSERT INTO review_settings(user_id,interview_date) VALUES (1,'2026-11-01')")
        if versioned:
            await c.exec_driver_sql("CREATE TABLE alembic_version(version_num VARCHAR(32) PRIMARY KEY)")
            await c.exec_driver_sql("INSERT INTO alembic_version VALUES ('0006')")
    with pytest.raises(RuntimeError, match="所有者冲突"):
        await init_db()
    assert await rows("SELECT id,username,password_hash FROM user") == [(1, "alice", "untouched")]
    assert await rows("SELECT user_id,interview_date FROM review_settings") == [(1, "2026-11-01")]
    await close_db()
    with pytest.raises(RuntimeError, match="所有者冲突"):
        migrate(bootstrap_db)
    assert await rows("SELECT id,username FROM user") == [(1, "alice")]


@async_test
async def test_head_without_admin_reserves_id_before_create_user(bootstrap_db):
    await init_db()
    async with get_engine().begin() as c:
        await c.exec_driver_sql("DELETE FROM user")
    await cmd_create_user(Namespace(username="alice", password="alice-password-123"))
    assert await rows("SELECT id,username FROM user ORDER BY id") == [(1, "admin"), (2, "alice")]


@async_test
async def test_fresh_direct_alembic_matches_runtime_bootstrap(bootstrap_db):
    migrate(bootstrap_db)
    assert await rows("SELECT id,username,password_hash FROM user") == [(1, "admin", "!locked")]
    async with get_engine().begin() as c:
        await c.exec_driver_sql("DELETE FROM user WHERE id=1")
    await close_db()
    migrate(bootstrap_db)  # Already-head direct Alembic must also restore the reservation.
    assert await rows("SELECT id,username,password_hash FROM user") == [(1, "admin", "!locked")]
    await init_db()
    assert await rows("SELECT id,username,password_hash FROM user") == [(1, "admin", "!locked")]


@pytest.mark.parametrize("baseline", ["fresh", "0005", "create_all"])
def test_real_cli_entrypoint_initializes_before_creating_user(bootstrap_db, baseline):
    """Exercise argv dispatch + common init/finally, not just cmd_create_user."""
    from drivecast import db
    from drivecast.__main__ import main

    if baseline == "0005":
        migrate(bootstrap_db, "0005")
    elif baseline == "create_all":
        engine = create_engine(f"sqlite:///{bootstrap_db}")
        Base.metadata.create_all(engine)
        engine.dispose()
    with mock.patch("sys.argv", ["drivecast", "create-user", "alice"]), \
            mock.patch.dict("os.environ", {"DRIVECAST_USER_PASSWORD": "cli-integration-password"}), \
            redirect_stdout(StringIO()):
        main()
    assert db._engine is None  # main's finally closed its temporary connection
    users = asyncio.run(rows("SELECT id,username,password_hash FROM user ORDER BY id"))
    assert [(u.id, u.username) for u in users] == [(1, "admin"), (2, "alice")]
    assert users[0].password_hash == "!locked"
    assert auth.verify_password("cli-integration-password", users[1].password_hash)
    assert asyncio.run(rows("SELECT version_num FROM alembic_version")) == [("0008",)]


@async_test
async def test_direct_alembic_repairs_missing_reserved_admin_at_head(bootstrap_db):
    migrate(bootstrap_db)
    async with get_engine().begin() as c:
        await c.exec_driver_sql("DELETE FROM user WHERE id=1")
    await close_db()
    migrate(bootstrap_db)
    assert await rows("SELECT id,username,password_hash FROM user") == [(1, "admin", "!locked")]


@async_test
async def test_admin_at_nonreserved_id_is_rejected_without_renumbering(bootstrap_db):
    async with get_engine().begin() as c:
        await c.run_sync(Base.metadata.create_all)
        await c.exec_driver_sql("INSERT INTO user(id,username,password_hash) VALUES (7,'admin','preserved')")
    with pytest.raises(RuntimeError, match="所有者冲突"):
        await init_db()
    assert await rows("SELECT id,username,password_hash FROM user") == [(7, "admin", "preserved")]


@async_test
async def test_unknown_unversioned_schema_refuses_without_stamping(bootstrap_db):
    async with get_engine().begin() as c:
        await c.exec_driver_sql("CREATE TABLE unknown_table(id INTEGER PRIMARY KEY, value TEXT)")
        await c.exec_driver_sql("INSERT INTO unknown_table VALUES (1,'preserved')")
    with pytest.raises(RuntimeError, match="不是完整已知 schema"):
        await init_db()
    assert await rows("SELECT * FROM unknown_table") == [(1, "preserved")]
    assert await rows("SELECT name FROM sqlite_master WHERE name IN ('alembic_version','user')") == []


@async_test
async def test_partial_replay_upgrade_preserves_existing_owner_and_primary_key(bootstrap_db):
    migrate(bootstrap_db, "0005")
    engine = create_engine(f"sqlite:///{bootstrap_db}")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE user(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        c.exec_driver_sql("INSERT INTO user(id,username,password_hash) VALUES (1,'admin','!locked'),(7,'alice','preserved')")
        c.exec_driver_sql("INSERT INTO topic(title,direction) VALUES ('legacy','backend_arch')")
        c.exec_driver_sql("INSERT INTO plan(ext_id,topic_id,title) VALUES ('legacy',1,'legacy')")
        c.exec_driver_sql("INSERT INTO episode(ext_id,plan_id,seq,title) VALUES ('legacy_ep',1,1,'legacy')")
        c.exec_driver_sql("ALTER TABLE review_replay ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        c.exec_driver_sql("INSERT INTO review_replay(id,user_id,kp_id,episode_id,scheduled_for,weak_question_count) VALUES (42,7,'kp',1,'2026-10-01',1)")
    engine.dispose()
    await init_db()
    assert await rows("SELECT id,user_id FROM review_replay") == [(42, 7)]
    assert await rows("SELECT password_hash FROM user WHERE id=7") == [("preserved",)]
    # New tenant-scoped uniqueness actually works after the table rebuild.
    async with get_engine().begin() as c:
        await c.exec_driver_sql("INSERT INTO review_replay(user_id,kp_id,episode_id,scheduled_for,weak_question_count) VALUES (1,'kp',1,'2026-10-01',1)")
    assert await rows("SELECT user_id FROM review_replay ORDER BY id") == [(7,), (1,)]
