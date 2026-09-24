"""Offline contract checks between loopzero's argv and the installed GitHub CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from loopzero import github
from loopzero.types import Finding, ReviewResult
from tests.test_github import (
    HEAD,
    PR_FILES,
    REPO,
    FakeGh,
    merge_json,
    pr_json,
    rest_pr_json,
    threads_json,
)

REAL_GH = shutil.which("gh")


@pytest.fixture
def captured_argv(fake_bin: Path, tmp_path: Path) -> list[dict]:
    """Drive every public operation through production code and retain its exact argv."""
    fake = FakeGh(fake_bin, tmp_path)
    merged = merge_json(state='MERGED', head=HEAD, sha='c' * 40)
    fake.respond_pr_list([pr_json()], branch="lz/x")
    fake.respond_pr_view(pr_json(), pr_json(), branch="lz/x")
    fake.respond_pr_create(pr_json(), branch="lz/x")
    fake.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})
    fake.append(f"api repos/{REPO}/pulls/7", merged)
    fake.respond("api graphql viewer", {"data": {"viewer": {"login": "bot-user"}}})
    fake.respond(f"api repos/{REPO}/pulls/7/files", PR_FILES)
    fake.respond(f"api repos/{REPO}/pulls/7/reviews", {"id": 1})
    fake.respond("api graphql", threads_json())
    fake.respond(f"api repos/{REPO}/commits/{HEAD}/check-runs", {"check_runs": []})
    fake.respond(f"api repos/{REPO}/commits/{HEAD}/statuses", [])
    fake.respond("pr ready", "")
    fake.respond("pr merge", "")
    fake.respond("api -X", "")

    github.pr_for_branch(REPO, "lz/x")
    github.pr_view(REPO, 7)
    github.create_draft_pr(REPO, "lz/x", "main", "Title", "Body\n")
    github.update_body(REPO, 7, "new body")
    result = ReviewResult(
        family="codex",
        head=HEAD,
        kind="primary",
        verdict="request_changes",
        findings=(Finding("important", "src/a.py", 10, "Problem", "Details"),),
        raw="raw",
    )
    github.post_review(REPO, 7, HEAD, result)
    github.open_blocking_findings(REPO, 7, HEAD)
    github.check_runs(REPO, HEAD)
    github.mark_ready(REPO, 7)
    github.merge(REPO, 7, "squash", HEAD)
    github.delete_remote_branch(REPO, "lz/x")
    return fake.calls


def offline_run(argv: list[str], tmp_path: Path, call: dict | None = None) -> str:
    if REAL_GH is None:
        pytest.skip("GitHub CLI (gh) is not installed")
    actual = list(argv)
    tmp_path.mkdir(parents=True, exist_ok=True)
    for flag, content in (call or {}).get("input_files", {}).items():
        path = tmp_path / f"input-{actual.index(flag)}"
        path.write_text(content)
        actual[actual.index(flag) + 1] = str(path)
    home = tmp_path / "home"
    config = tmp_path / "gh-config"
    home.mkdir(exist_ok=True)
    config.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GH_CONFIG_DIR": str(config),
        "GH_HOST": "127.0.0.1",
        "GH_TOKEN": "offline-contract-token",
        "GH_ENTERPRISE_TOKEN": "offline-contract-token",
        "GH_DEBUG": "api",
        "NO_PROXY": "127.0.0.1",
        "LANG": "C.UTF-8",
    }
    done = subprocess.run(
        [REAL_GH, *actual], capture_output=True, text=True, env=env, timeout=10, check=False
    )
    return done.stderr + "\n" + done.stdout


def test_contract_capture_covers_all_github_operations(captured_argv: list[dict]) -> None:
    calls = [call["argv"] for call in captured_argv]
    assert {tuple(argv[:2]) for argv in calls} >= {
        ("pr", "ready"), ("pr", "merge"), ("api", "graphql"), ("api", "-X"),
        ("api", f"repos/{REPO}/pulls"), ("api", f"repos/{REPO}/pulls/7"),
    }
    assert not any(argv[:2] in (["pr", "list"], ["pr", "view"], ["pr", "create"])
                   for argv in calls)
    assert any("/files" in argv[1] for argv in calls if argv[0] == "api")
    assert any("/check-runs" in argv[1] for argv in calls if argv[0] == "api")
    assert any("/statuses" in argv[1] for argv in calls if argv[0] == "api")


def test_pr_metadata_uses_rest_without_graphql_fields(captured_argv: list[dict]) -> None:
    calls = [call["argv"] for call in captured_argv]
    assert not any("--json" in argv for argv in calls)
    assert any("?state=open&head=acme%3Alz%2Fx" in argv[1] for argv in calls)


def test_captured_api_methods_match_the_wire(
    captured_argv: list[dict], tmp_path: Path
) -> None:
    api_calls = [call for call in captured_argv if call["argv"][0] == "api"]
    for index, call in enumerate(api_calls):
        argv = call["argv"]
        if any(flag in argv for flag in ("-F", "-f")) and argv[1] != "graphql":
            assert argv[argv.index("--method") + 1] == "GET", argv

        if argv[1] == "graphql":
            expected = "POST"  # GitHub's GraphQL read transport is intentionally POST.
        elif argv[1] == "-X":
            expected = argv[2]
        elif "--method" in argv:
            expected = argv[argv.index("--method") + 1]
        elif "--input" in argv:
            expected = "POST"
        else:
            expected = "GET"
        output = offline_run(argv, tmp_path / str(index), call)
        methods = re.findall(r"^> (GET|POST|PATCH|PUT|DELETE) ", output, re.MULTILINE)
        assert methods and methods[0] == expected, (argv, expected, output)


@pytest.mark.parametrize("command", ["ready", "merge"])
def test_captured_pr_mutations_use_graphql_post(
    captured_argv: list[dict], tmp_path: Path, command: str
) -> None:
    call = next(item for item in captured_argv if item["argv"][:2] == ["pr", command])
    output = offline_run(call["argv"], tmp_path, call)
    assert re.search(r"^> POST ", output, re.MULTILINE), output


def test_strict_fake_rejects_exhausted_calls(
    fake_bin: Path, tmp_path: Path
) -> None:
    fake = FakeGh(fake_bin, tmp_path)
    fake.expect(
        ["api", f"repos/{REPO}/pulls/7"], rest_pr_json(pr_json()),
    )
    assert github.pr_view(REPO, 7).number == 7
    fake.assert_complete()
    with pytest.raises(github.GhError, match="exhausted"):
        github.pr_view(REPO, 7)


def test_strict_fake_rejects_unexpected_argv(fake_bin: Path, tmp_path: Path) -> None:
    fake = FakeGh(fake_bin, tmp_path)
    fake.expect(["pr", "ready", "8", "--repo", REPO])
    with pytest.raises(github.GhError, match="unexpected argv"):
        github.mark_ready(REPO, 7)


def test_strict_fake_matches_input_contents(fake_bin: Path, tmp_path: Path) -> None:
    fake = FakeGh(fake_bin, tmp_path)
    fake.expect(
        ["api", f"repos/{REPO}/pulls/7", "--method", "PATCH", "--input", "<temp-file>"],
        {},
        input_files={"--input": json.dumps({"body": "new body"})},
    )
    github.update_body(REPO, 7, "new body")
    fake.assert_complete()
