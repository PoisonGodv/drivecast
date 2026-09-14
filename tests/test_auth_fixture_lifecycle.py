"""Explicit fixture cleanup must preserve caller state under both runners."""
import unittest
from unittest import mock

from auth_test_support import real_auth_scope
from drivecast.auth import get_current_user
from drivecast.config import settings
from drivecast.web.main import app
from drivecast.web import api_auth
import test_login_limits as limits_tests


class AuthFixtureLifecycleTest(unittest.TestCase):
    def test_real_auth_scope_restores_existing_overrides_and_settings_on_failure(self):
        sentinel = object()
        with mock.patch.dict(app.dependency_overrides, {get_current_user: sentinel}, clear=True), \
                mock.patch.object(settings, "auth_secret", "outer-test-secret"), \
                mock.patch.object(settings, "auth_token_expire_days", 3):
            case = unittest.TestCase()
            try:
                real_auth_scope(case)
                self.assertEqual(app.dependency_overrides, {})
                self.assertNotEqual(settings.auth_secret, "outer-test-secret")
                self.assertEqual(settings.auth_token_expire_days, 7)
                raise RuntimeError("setup failed")
            except RuntimeError:
                case.doCleanups()
            self.assertEqual(app.dependency_overrides, {get_current_user: sentinel})
            self.assertEqual(settings.auth_secret, "outer-test-secret")
            self.assertEqual(settings.auth_token_expire_days, 3)

    def test_limiter_cases_restore_existing_state_in_either_order(self):
        sentinel = api_auth._LoginState(fails=3, until=123)
        original_time = api_auth.time
        original_session = api_auth.session
        original_tasks = api_auth._login_tasks
        with mock.patch.dict(api_auth._login_failures, {"outer": sentinel}, clear=True):
            for kind, name in [
                (limits_tests.LoginStateTest, "test_threaded_admission_is_atomic"),
                (limits_tests.LoginApiLimitsTest, "test_unknown_user_keeps_dummy_hash_and_uniform_failure"),
                (limits_tests.LoginStateTest, "test_lock_boundary_backoff_and_bounded_counter"),
            ]:
                with self.subTest(case=name):
                    result = unittest.TestResult()
                    kind(name).run(result)
                    self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
                    self.assertEqual(api_auth._login_failures, {"outer": sentinel})
                    self.assertIs(api_auth.time, original_time)
                    self.assertIs(api_auth.session, original_session)
                    self.assertIs(api_auth._login_tasks, original_tasks)
