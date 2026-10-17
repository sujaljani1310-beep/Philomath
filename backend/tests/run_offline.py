"""Run tests with dotenv disabled, credential bindings removed, and sockets blocked."""

import os
import socket
import sys
import unittest
from pathlib import Path


def main() -> int:
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for name in (
        "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY",
        "PHILOMATH_ENCRYPTION_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY",
        "NVIDIA_API_KEY", "GEMINI_API_KEY", "GROK_API_KEY", "GROK_MODEL",
        "PHILOMATH_STAGING_DATABASE_URL", "PHILOMATH_STAGING_SERVICE_ROLE_KEY",
        "PHILOMATH_STAGING_PUBLIC_KEY", "SUPABASE_ACCESS_TOKEN",
        "PHILOMATH_STAGING_MANAGEMENT_TOKEN",
    ):
        os.environ.pop(name, None)

    def blocked(*args, **kwargs):
        raise AssertionError("Network access is forbidden in offline backend tests")

    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.getaddrinfo = blocked
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
