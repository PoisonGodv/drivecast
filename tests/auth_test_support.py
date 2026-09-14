"""Explicit unittest/pytest authentication fixtures; no import-time overrides.

Call only after a test has selected its temporary data_dir and initialized DB.
Business API tests use a real persisted admin and signed Bearer credentials;
authentication tests keep the actual dependency and select their own users.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from drivecast import db
from drivecast.auth import create_token, hash_password
from drivecast.config import settings
from drivecast.models import User
from drivecast.web.main import app


def real_auth_scope(case) -> None:
    """Restore dependencies/settings even on setUp failure (unittest addCleanup)."""
    case.enterContext(patch.dict(app.dependency_overrides, {}, clear=True))
    case.enterContext(patch.object(settings, "auth_secret", "test-only-auth-secret-not-for-deployment-2026"))
    case.enterContext(patch.object(settings, "auth_token_expire_days", 7))


async def enable_test_admin() -> int:
    """Unlock the temporary bootstrap admin, preserving its reserved identity."""
    async with db.session() as s:
        admin = await s.scalar(select(User).where(User.username == "admin"))
        if admin is None:
            raise AssertionError("init_db must seed the reserved admin before business fixtures")
        admin.password_hash = hash_password("temporary-business-fixture-password")
        await s.commit()
        return admin.id


def authenticated_admin_client(case) -> TestClient:
    real_auth_scope(case)
    user_id = asyncio.run(enable_test_admin())
    # Legacy business fixtures intentionally exercise default admin ownership.
    case.assertEqual(user_id, 1, "legacy ownership must belong to reserved admin")
    token, _ = create_token(user_id)
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    case.addCleanup(client.close)
    return client
