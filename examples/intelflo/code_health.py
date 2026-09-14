"""Prepared IntelFlo adapter; adopt after its owning package-cutover PR.

Run from the approved consumer checkout/environment, inside its validation
containment. This file owns policy and status; loop-zero owns measurements.
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def policy_for(target):
    scope = []
    if target in ("python", "all"):
        scope.extend(["fastapi_backend/app", "fastapi_backend/tests"])
    if target in ("next", "all"):
        scope.extend(
            [
                "nextjs-frontend/app",
                "nextjs-frontend/components",
                "nextjs-frontend/lib",
                "nextjs-frontend/__tests__",
            ]
        )
    return {
        "scope": sorted(scope),
        "exclude": [
            "*/node_modules/*",
            "*/generated/*",
            "nextjs-frontend/app/openapi-client/*",
            "*/__fixtures__/*",
            "*/fixtures/*",
            "fastapi_backend/app/scripts/*",
        ],
        "test_root": ["fastapi_backend/tests", "nextjs-frontend/__tests__"],
        "test_pattern": [
            "*/__tests__/*",
            "*.test.ts",
            "*.test.tsx",
            "*.spec.ts",
            "*.spec.tsx",
        ],
        "signals": ["size", "complexity", "duplication"],
        "ruff_version": "0.5.7",
        "min_lines": 15,
        "min_tokens": 100,
        "clone_mode": "weak",
    }


def write_json(path, payload):
    """Replace atomically; the health runner serializes status writers."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def register_status(path, record, *, head, run_id, target):
    status = json.loads(path.read_text(encoding="utf-8"))
    if status.get("git_head") != head or status.get("run_id") != run_id:
        raise ValueError("health status belongs to a different source/run")
    collectors = status.setdefault("collectors", {})
    collectors.setdefault(target, {})["code_structure"] = {**record, "run_id": run_id}
    write_json(path, status)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--head", required=True, help="expected full commit SHA")
    parser.add_argument("--base", help="expected full base SHA for advisory PR mode")
    parser.add_argument("--target", choices=("python", "next", "all"), default="all")
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument(
        "--status", type=Path, help="existing health status.json; omit for PR evidence"
    )
    args = parser.parse_args(argv)
    for sha in filter(None, (args.head, args.base)):
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            parser.error("source identities must be full commit SHAs")
    if args.status and args.base:
        parser.error("health status accepts snapshot mode only")
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    artifact = args.audit_dir / "code-structure.json"
    report_path = args.audit_dir / "code-structure.md"
    policy = policy_for(args.target)
    run_id = None
    if args.status:
        status = json.loads(args.status.read_text(encoding="utf-8"))
        run_id = status.get("run_id")
        if (
            status.get("git_head") != args.head
            or not isinstance(run_id, str)
            or not run_id
        ):
            raise ValueError("health status has no matching source/run")
    try:
        from loopzero.code_health.cli import markdown
        from loopzero.code_health.collector import collect
        from loopzero.code_health.evidence import collector_status

        report = collect(args.root, args.head, policy, args.base)
        record = collector_status(
            report,
            exit_code=0 if report["valid"] else 1,
            head=args.head,
            base=args.base,
            policy=policy,
        )
        rendered = markdown(report)
    except (ImportError, OSError, ValueError, subprocess.SubprocessError) as exc:
        report = {
            "schema_version": 1,
            "valid": False,
            "threshold_failed": False,
            "errors": [str(exc)],
        }
        record = {
            "valid": False,
            "threshold_failed": False,
            "exit_code": 1,
            "git_head": args.head,
            "reason": str(exc),
        }
        rendered = f"Code structure collection INCOMPLETE: {exc}\n"
    write_json(artifact, report)
    report_path.write_text(rendered, encoding="utf-8")
    record.update(
        {
            "artifact": str(artifact),
            "artifact_exists": True,
            "artifact_bytes": artifact.stat().st_size,
            "command": "IntelFlo code_health.py "
            + ("compare" if args.base else "snapshot"),
        }
    )
    if args.status:
        register_status(
            args.status, record, head=args.head, run_id=run_id, target=args.target
        )
    return 0 if record["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
