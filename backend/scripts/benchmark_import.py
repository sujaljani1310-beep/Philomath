"""Compare clean-process imports against a Git revision, without any network I/O."""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
IMPORT_SCRIPT = """
import json, socket, time
def blocked(*args, **kwargs):
    raise AssertionError('Network disabled for import benchmark')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked
started = time.perf_counter()
from app.main import app
print(json.dumps({'seconds': time.perf_counter() - started}))
"""


def measure(directory: Path, runs: int) -> dict:
    env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = str(directory)
    for name in (
        "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY",
        "PHILOMATH_ENCRYPTION_KEY", "GEMINI_API_KEY", "GROK_API_KEY",
        "CEREBRAS_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY",
    ):
        env.pop(name, None)
    samples = []
    for _ in range(runs):
        result = subprocess.run(
            [sys.executable, "-B", "-c", IMPORT_SCRIPT], cwd=directory,
            env=env, check=True, text=True, capture_output=True,
        )
        samples.append(json.loads(result.stdout)["seconds"])
    return {"samples_seconds": samples, "median_seconds": statistics.median(samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="HEAD")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", args.baseline_ref + "^{commit}"],
        cwd=ROOT, text=True,
    ).strip()
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "-z", commit, "--", "backend/app"], cwd=ROOT,
    ).decode().split("\0")
    with tempfile.TemporaryDirectory(prefix="philomath-import-") as directory:
        baseline = Path(directory)
        for name in filter(None, paths):
            relative = Path(name).relative_to("backend")
            target = baseline / relative
            if not target.resolve().is_relative_to(baseline.resolve()):
                raise ValueError("Invalid baseline source path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT))
        print(json.dumps({
            "baseline_commit": commit,
            "baseline": measure(baseline, args.runs),
            "working_tree": measure(BACKEND, args.runs),
            "note": "Local import only; deferred SDK work occurs on first use. Not Render cold-start latency.",
        }, indent=2))


if __name__ == "__main__":
    main()
