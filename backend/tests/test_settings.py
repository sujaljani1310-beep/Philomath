import os
import unittest
from unittest.mock import patch

from app.settings import get_settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()
        self.addCleanup(get_settings.cache_clear)
        active = patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "1"}, clear=True)
        active.start()
        self.addCleanup(active.stop)

    def test_defaults_and_environment_overrides(self):
        self.assertEqual(get_settings().database_timeout_seconds, 15)
        self.assertEqual(get_settings().auth_timeout_seconds, 10)
        self.assertEqual(get_settings().provider_max_retries, 0)
        get_settings.cache_clear()
        os.environ["PHILOMATH_DATABASE_TIMEOUT_SECONDS"] = "7.5"
        os.environ["PHILOMATH_PERFORMANCE_LOGGING"] = "false"
        self.assertEqual(get_settings().database_timeout_seconds, 7.5)
        self.assertFalse(get_settings().performance_logging_enabled)

    def test_invalid_timeouts_fail_without_echoing_value(self):
        for value in ("0", "-1", "nan", "inf", "secret-invalid-value"):
            with self.subTest(value=value):
                get_settings.cache_clear()
                os.environ["PHILOMATH_AUTH_TIMEOUT_SECONDS"] = value
                with self.assertRaisesRegex(ValueError, "positive finite") as caught:
                    get_settings()
                self.assertNotIn(value, str(caught.exception))

    def test_retries_cannot_be_negative_or_unbounded(self):
        for value in ("-1", "3", "1.5"):
            get_settings.cache_clear()
            os.environ["PHILOMATH_PROVIDER_MAX_RETRIES"] = value
            with self.assertRaisesRegex(ValueError, "between 0 and 2"):
                get_settings()
