#!/usr/bin/env python3
"""Record a small, read-only GitHub CLI contract corpus.

Run only against a PR that is safe to inspect, for example:
    python scripts/record_gh.py feder-positronics/loop-zero 125
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PR_FIELDS = "number,url,headRefOid,baseRefName,isDraft,state,mergeable,author"
THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved isOutdated path line
          comments(first: 1) { nodes { body } }
        }
      }
    }
  }
}
"""
SHA_RE = re.compile(r"\b[0-9a-fA-F]{40}\b")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")


class Redactor:
    def __init__(self, repo: str, pr: int) -> None:
        self.repo = repo
        self.owner, self.name = repo.split("/", 1)
        self.pr = str(pr)
        self.values: dict[str, dict[str, str]] = {
            "LOGIN": {}, "NODE_ID": {}, "URL": {}, "SHA": {},
        }

    def token(self, kind: str, value: object) -> str:
        text = str(value)
        known = self.values[kind]
        if text not in known:
            known[text] = f"<{kind}_{len(known) + 1}>"
        return known[text]

    def text(self, value: str) -> str:
        value = value.replace(self.repo, "<REPO>")
        value = SHA_RE.sub(lambda match: self.token("SHA", match.group()), value)
        value = URL_RE.sub(lambda match: self.token("URL", match.group()), value)
        return value

    def data(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {name: self.data(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [self.data(item, key) for item in value]
        if key == "login" and value is not None:
            return self.token("LOGIN", value)
        if key == "number" and str(value) == self.pr:
            return "<PR>"
        if key in {"node_id", "endCursor"} and value is not None:
            return self.token("NODE_ID", value)
        if key == "id" and isinstance(value, str) and not value.isdigit():
            return self.token("NODE_ID", value)
        if isinstance(value, str):
            if ("url" in key.lower() or key == "href") and value:
                return self.token("URL", value)
            return self.text(value)
        return value

    def output(self, value: str) -> str:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return self.text(value)
        return json.dumps(self.data(parsed), separators=(",", ":")) + ("\n" if value.endswith("\n") else "")

    def argv(self, argv: list[str]) -> list[str]:
        result: list[str] = []
        temp_index = 0
        temp_next = False
        for arg in argv:
            if temp_next:
                temp_index += 1
                result.append(f"<TEMP_FILE_{temp_index}>")
                temp_next = False
                continue
            clean = arg.replace(self.repo, "<REPO>")
            clean = clean.replace(f"owner={self.owner}", "owner=<OWNER>")
            clean = clean.replace(f"name={self.name}", "name=<NAME>")
            if clean == self.pr:
                clean = "<PR>"
            clean = re.sub(rf"(?<=pulls/){re.escape(self.pr)}(?=/|$)", "<PR>", clean)
            clean = re.sub(rf"(?<=number=){re.escape(self.pr)}(?=$)", "<PR>", clean)
            result.append(self.text(clean))
            temp_next = arg in {"--input", "--body-file"}
        return result


def run_gh(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *argv], capture_output=True, text=True, check=False)


def input_files(argv: list[str], redactor: Redactor) -> dict[str, str]:
    found: dict[str, str] = {}
    for flag in ("--input", "--body-file"):
        if flag in argv:
            path = Path(argv[argv.index(flag) + 1])
            found[flag] = redactor.output(path.read_text())
    return found


def commands(repo: str, pr: int, head_ref: str, head_sha: str) -> dict[str, list[str]]:
    owner, name = repo.split("/", 1)
    return {
        "pr_view_merged": [
            "pr", "view", str(pr), "--repo", repo, "--json",
            "mergeCommit,state,headRefName,headRefOid",
        ],
        "pr_list_by_head": [
            "pr", "list", "--repo", repo, "--head", head_ref, "--state", "all",
            "--limit", "10", "--json", PR_FIELDS,
        ],
        "user": ["api", "user"],
        "files": [
            "api", f"repos/{repo}/pulls/{pr}/files", "--method", "GET",
            "-F", "per_page=100", "-F", "page=1",
        ],
        "review_threads": [
            "api", "graphql", "-f", f"query={THREADS_QUERY}", "-F", f"owner={owner}",
            "-F", f"name={name}", "-F", f"number={pr}",
        ],
        "check_runs": [
            "api", f"repos/{repo}/commits/{head_sha}/check-runs", "--method", "GET",
            "-F", "per_page=100", "-F", "page=1",
        ],
        "statuses": [
            "api", f"repos/{repo}/commits/{head_sha}/statuses", "--method", "GET",
            "-F", "per_page=100", "-F", "page=1",
        ],
        "reviews_list": ["api", f"repos/{repo}/pulls/{pr}/reviews?per_page=100&page=1"],
    }


def metadata(repo: str, pr: int) -> tuple[str, str]:
    done = run_gh(["pr", "view", str(pr), "--repo", repo, "--json", "headRefName,headRefOid"])
    if done.returncode:
        raise SystemExit(done.stderr or done.stdout)
    data = json.loads(done.stdout)
    return data["headRefName"], data["headRefOid"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("pr", type=int)
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--output", type=Path, default=Path("tests/fixtures/gh"))
    args = parser.parse_args()

    version = run_gh(["--version"])
    if version.returncode:
        raise SystemExit(version.stderr or "gh is unavailable")
    head_ref, head_sha = metadata(args.repo, args.pr)
    available = commands(args.repo, args.pr, head_ref, head_sha)
    selected = args.scenarios or list(available)
    unknown = sorted(set(selected) - set(available))
    if unknown:
        raise SystemExit(f"unknown scenarios: {', '.join(unknown)}")

    args.output.mkdir(parents=True, exist_ok=True)
    redactor = Redactor(args.repo, args.pr)
    recorded_at = datetime.now(UTC).isoformat()
    gh_version = version.stdout.splitlines()[0]
    for scenario in selected:
        argv = available[scenario]
        done = run_gh(argv)
        record = {
            "argv": redactor.argv(argv),
            "exit_code": done.returncode,
            "stdout": redactor.output(done.stdout),
            "stderr": redactor.output(done.stderr),
            "input_files": input_files(argv, redactor),
            "gh_version": gh_version,
            "recorded_at": recorded_at,
            "scenario": scenario,
        }
        (args.output / f"{scenario}.json").write_text(json.dumps(record, indent=2) + "\n")
        if done.returncode:
            print(f"{scenario}: exit {done.returncode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
