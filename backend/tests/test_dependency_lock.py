"""Offline checks that pins reproduce the tested runtime dependency closure."""

import importlib.metadata as metadata
from pathlib import Path
import platform
import sys
import unittest

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name


BACKEND = Path(__file__).resolve().parents[1]
DIRECT_DEPENDENCIES = {
    "fastapi", "uvicorn", "python-dotenv", "supabase", "openai",
    "google-genai", "python-docx", "pypdf", "python-multipart",
    "cryptography", "httpx",
}
TESTED_TARGET = (
    sys.implementation.name == "cpython"
    and sys.version_info[:2] == (3, 12)
    and platform.system() == "Linux"
    and platform.machine() == "x86_64"
)


def requirement_lines(path):
    return [
        line.strip() for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class DependencyLockTests(unittest.TestCase):
    def setUp(self):
        self.direct_lines = requirement_lines(BACKEND / "requirements.txt")
        self.lock_lines = requirement_lines(BACKEND / "requirements.lock")

    def test_existing_direct_dependencies_and_extras_are_exactly_pinned(self):
        self.assertEqual(
            [line for line in self.direct_lines if line.startswith("-")],
            ["-c requirements.lock"],
        )
        direct = [Requirement(line) for line in self.direct_lines if not line.startswith("-")]
        self.assertEqual({canonicalize_name(req.name) for req in direct}, DIRECT_DEPENDENCIES)
        self.assertEqual(len(direct), len(DIRECT_DEPENDENCIES))
        for req in direct:
            with self.subTest(dependency=req.name):
                self.assertIsNone(req.marker)
                self.assertIsNone(req.url)
                self.assertEqual(req.extras, {"standard"} if req.name == "uvicorn" else set())
                specs = list(req.specifier)
                self.assertEqual(len(specs), 1)
                self.assertEqual(specs[0].operator, "==")
                self.assertNotIn("*", specs[0].version)

    @unittest.skipUnless(TESTED_TARGET, "runtime lock is validated for CPython 3.12 / Linux x86_64")
    def test_lock_matches_complete_installed_runtime_closure(self):
        locked = {}
        for line in self.lock_lines:
            req = Requirement(line)
            name = canonicalize_name(req.name)
            self.assertNotIn(name, locked, "duplicate lock entry")
            self.assertFalse(req.extras)
            self.assertIsNone(req.marker)
            self.assertIsNone(req.url)
            specs = list(req.specifier)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].operator, "==")
            self.assertNotIn("*", specs[0].version)
            locked[name] = specs[0].version

        installed = {
            canonicalize_name(dist.metadata["Name"]): dist
            for dist in metadata.distributions()
        }
        environment = default_environment()
        pending = [Requirement(line) for line in self.direct_lines if not line.startswith("-")]
        reached = {}
        while pending:
            req = pending.pop()
            name = canonicalize_name(req.name)
            with self.subTest(dependency=name):
                self.assertIn(name, locked, "active dependency lacks an exact constraint")
                self.assertIn(name, installed)
                dist = installed[name]
                self.assertEqual(dist.version, locked[name], "installed version differs from lock")
                self.assertIn(dist.version, req.specifier, "active dependency constraint is not met")
                python_constraint = dist.metadata.get("Requires-Python")
                if python_constraint:
                    self.assertIn(platform.python_version(), SpecifierSet(python_constraint))

            selected_extras = reached.get(name, set()) | req.extras
            if name in reached and selected_extras == reached[name]:
                continue
            reached[name] = selected_extras
            for declaration in dist.requires or []:
                child = Requirement(declaration)
                if child.marker is None or any(
                    child.marker.evaluate({**environment, "extra": extra})
                    for extra in {""} | selected_extras
                ):
                    pending.append(child)

        self.assertEqual(set(locked), set(reached), "lock must contain only the full runtime closure")
        self.assertNotIn("pip", locked)


if __name__ == "__main__":
    unittest.main()
