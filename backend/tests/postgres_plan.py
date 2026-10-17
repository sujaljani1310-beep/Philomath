"""Non-executing PostgreSQL test planner. No driver, DSN, sockets or subprocess.

Canonical evidence is supplied offline; presence is not semantic validation.
There is deliberately no execution switch or automatic environment discovery.
"""

import argparse
import hashlib
import json
from pathlib import Path


REQUIRED_EVIDENCE = (
    "columns", "constraints", "indexes", "triggers", "functions", "policies",
    "table_and_column_grants", "role_membership_and_ownership", "postgres_version",
)


def build_plan(metadata=None):
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("Canonical evidence must be a JSON object")
    metadata = metadata or {}
    missing = [name for name in REQUIRED_EVIDENCE if name not in metadata or metadata[name] is None]
    return {
        "execution_enabled": False,
        "status": "BLOCKED" if missing else "REVIEW_REQUIRED",
        "missing_evidence": missing,
        "schema_verified": False,
        "required_target": "Disposable offline fixture or verified staging tdiqzmaqvoynspmilwra; never production",
        "sql_scaffolds": ["postgres/catalog_contract.sql", "postgres/two_user_permissions.sql"],
        "requires": [
            "Scoped offline/staging execution is approved; this historical planner still cannot execute",
            "Human verification of canonical DDL, grants and delete semantics",
            "Synthetic Auth UUIDs A/B provisioned in the disposable fixture",
            "Reviewed baseline/hardened fixture loading and cleanup procedure",
            "Independent transaction sessions for race tests; real PostgREST checks separately",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, help="Already supplied sanitized schema-only JSON; never a URL/DSN")
    args = parser.parse_args(argv)
    metadata = None
    digest = None
    if args.metadata is not None:
        if args.metadata.is_symlink() or not args.metadata.is_file() or args.metadata.stat().st_size > 2 * 1024 * 1024:
            parser.error("Use a regular sanitized metadata file of at most 2 MiB")
        with args.metadata.open("rb") as supplied:
            raw = supplied.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            parser.error("Use metadata of at most 2 MiB")
        digest = hashlib.sha256(raw).hexdigest()
        metadata = json.loads(raw)
    result = build_plan(metadata)
    result["artifact_sha256"] = digest
    print(json.dumps(result, indent=2))  # Never print function bodies or grants supplied in metadata.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
