import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import main
from app.providers import clients


class TransportLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_async_wrapper_and_database_close_after_sync_cleanup_failure(self):
        await clients.close_provider_async_clients()
        owned = None
        close_database = Mock()
        close_sync = Mock(side_effect=RuntimeError("synthetic cleanup failure"))
        with patch.object(main, "get_settings", return_value=SimpleNamespace(
            performance_logging_enabled=False
        )), patch.object(clients, "close_provider_clients", close_sync), patch.object(
            main, "close_supabase_client", close_database
        ):
            try:
                with self.assertRaisesRegex(RuntimeError, "synthetic cleanup failure"):
                    async with main.lifespan(main.app):
                        owned = clients.gemini_unused_async_client()
                        self.assertFalse(owned.is_closed)
                self.assertTrue(owned.is_closed)
                self.assertIsNone(clients._unused_async_client)
                close_sync.assert_called_once_with()
                close_database.assert_called_once_with()
            finally:
                await clients.close_provider_async_clients()

    async def test_logging_cleanup_runs_after_database_cleanup_failure(self):
        close_logging = Mock(return_value=True)
        with patch.object(main, "get_settings", return_value=SimpleNamespace(
            performance_logging_enabled=False
        )), patch.object(clients, "close_provider_clients"), patch.object(
            main, "close_supabase_client", side_effect=RuntimeError("synthetic database cleanup failure")
        ), patch.object(main, "shutdown_performance_logging", close_logging):
            with self.assertRaisesRegex(RuntimeError, "synthetic database cleanup failure"):
                async with main.lifespan(main.app):
                    pass
        close_logging.assert_called_once_with()
