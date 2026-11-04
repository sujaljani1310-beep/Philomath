# Backend dependency set

`requirements.txt` retains the same 11 direct dependencies and the
`uvicorn[standard]` extra. Each direct dependency is pinned to the version
already installed in the tested isolated environment. Its
`-c requirements.lock` directive also constrains every transitive runtime
dependency to its installed version. Install using the existing entry point:

```sh
python -m pip install -r backend/requirements.txt
```

This set was captured from **CPython 3.12.14 on Linux x86_64**. The lock contains
61 runtime distributions, including dependencies selected by transitive
extras (`httpx[http2]`, `google-auth[requests]`, and `pyjwt[crypto]`). The only
other installed distribution was `pip`; installer and unrelated development
tools are not included. Both `httpx` and `httpx2` are intentional: the installed
Supabase/Gemini dependencies use the former and OpenAI uses the latter.

The offline dependency tests traverse installed `Requires-Dist` metadata,
including active environment markers and extras. They check the complete
closure against the lock, installed versions, dependency version constraints,
and `Requires-Python`. Run them from `backend`:

```sh
.venv/bin/python -B -m unittest tests.test_dependency_lock -v
.venv/bin/python -B -m pip check
```

No packages were installed or upgraded to create this set. No package-index
availability check, clean reinstall, wheel-hash verification, or vulnerability
advisory scan was performed. Exact version pins make dependency resolution
reproducible for the tested target; they do not guarantee identical artifacts,
availability, or absence of vulnerabilities. Constraints do not install
packages by themselves, so do not install the lock file in place of
`requirements.txt`. Other Python versions, operating systems, architectures,
and newly requested extras require a separately validated dependency set.

For a future dependency update, change the relevant direct pin, resolve and
record its complete runtime closure in an isolated environment, then run the
offline regression suite and dependency metadata checks. Review release notes
and advisories separately before approving the update.
