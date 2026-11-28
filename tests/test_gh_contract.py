"""Offline contract checks between loopzero's argv and the installed GitHub CLI."""

from __future__ import annotations

import errno
import os
import re
import resource
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

from loopzero import github
from loopzero.types import Finding, ReviewResult
from tests.conftest import isolate_github_auth
from tests.test_github import (
    HEAD,
    REPO,
    FakeGh,
    gh as gh,
    merge_json,
    pr_json,
    arm_review,
    arm_workflow_check,
)

REAL_GH = shutil.which("gh")


def test_api_removes_payload_when_staging_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(github, "_gh_json", lambda *args, **kwargs: pytest.fail("unexpected dispatch"))
    original_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, original_limit[1]))
        with pytest.raises(OSError) as failure:
            github._api("repos/example/project/issues", {"body": "synthetic"})
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, original_limit)
        signal.signal(signal.SIGXFSZ, handler)
    assert failure.value.errno == errno.EFBIG
    assert not list(tmp_path.glob("*.loopzero"))


@pytest.fixture(scope="module")
def captured_argv(tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    """Drive every public operation through production code and retain its exact argv."""
    tmp_path = tmp_path_factory.mktemp("gh-contract")
    fake = FakeGh(tmp_path, tmp_path)
    merged = merge_json(state='MERGED', head=HEAD, sha='c' * 40)
    fake.respond_pr_list([pr_json()], branch="lz/x")
    fake.respond_pr_create(pr_json(), branch="lz/x")
    arm_review(fake, branch="lz/x")
    fake.respond_pr_view(pr_json(), branch="lz/x")
    arm_workflow_check(fake)
    fake.respond(f"api repos/{REPO}/pulls/7 --method PATCH", {})
    fake.append(f"api repos/{REPO}/pulls/7", merge_json(), merged)
    fake.respond("api graphql ready", "")
    fake.respond("pr merge", "")
    fake.fail(f"api repos/{REPO}/git/ref/heads/lz/x", "gh: Not Found (HTTP 404)")

    with pytest.MonkeyPatch.context() as env:
        env.setenv("PATH", str(tmp_path), prepend=os.pathsep)
        isolate_github_auth(tmp_path, env)
        github.pr_for_branch(REPO, "lz/x")
        github.pr_view(REPO, 7)
        github.create_draft_pr(REPO, "lz/x", "main", "Title", "Body\n")
        github.update_body(REPO, 7, "new body")
        result = ReviewResult("codex", HEAD, "primary", "request_changes",
                              (Finding("important", "src/a.py", 10, "Problem", "Details"),), "raw")
        github.post_review(REPO, 7, HEAD, result)
        github.open_blocking_findings(REPO, 7, HEAD, ("bot-user",))
        github._check_signals(REPO, HEAD)
        github.mark_ready(REPO, 7, "PR_7")
        github.merge(REPO, 7, "squash", HEAD, "main")
        github.delete_remote_branch(REPO, "lz/x", HEAD)
    return fake.calls


def offline_run(argv: list[str], tmp_path: Path, call: dict) -> str:
    if REAL_GH is None:
        pytest.skip("GitHub CLI (gh) is not installed")
    actual = list(argv)
    tmp_path.mkdir()
    for flag, content in call.get("input_files", {}).items():
        index = actual.index(flag) + 1
        path = tmp_path / f"input-{index}"
        path.write_text(content)
        actual[index] = str(path)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GH_CONFIG_DIR": str(tmp_path),
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


def test_contract_capture_covers_operations_with_rest_metadata(captured_argv: list[dict]) -> None:
    calls = [call["argv"] for call in captured_argv]
    assert {tuple(argv[:2]) for argv in calls} >= {
        ("pr", "merge"), ("api", "graphql"),
        ("api", f"repos/{REPO}/pulls"), ("api", f"repos/{REPO}/pulls/7"),
    }
    assert not any(argv[:2] in (["pr", "list"], ["pr", "view"], ["pr", "create"])
                   for argv in calls)
    assert any("/files" in argv[1] for argv in calls if argv[0] == "api")
    assert any("/check-runs" in argv[1] for argv in calls if argv[0] == "api")
    assert any("/statuses" in argv[1] for argv in calls if argv[0] == "api")

    assert not any("--json" in argv for argv in calls)
    assert any("?state=open&head=acme%3Alz%2Fx" in argv[1] for argv in calls)


def test_captured_methods_match_the_wire(captured_argv: list[dict], tmp_path: Path) -> None:
    for index, call in enumerate(captured_argv):
        argv = call["argv"]
        if argv[:2] == ["pr", "merge"] or argv[1] == "graphql":
            expected = "POST"
        elif "--method" in argv or argv[1] == "-X":
            flag = "-X" if argv[1] == "-X" else "--method"
            expected = argv[argv.index(flag) + 1]
            if "-F" in argv or "-f" in argv:
                assert flag == "--method" and expected == "GET", argv
        else:
            assert "-F" not in argv and "-f" not in argv, argv
            expected = "POST" if "--input" in argv else "GET"
        output = offline_run(argv, tmp_path / str(index), call)
        methods = re.findall(r"^> (GET|POST|PATCH|PUT|DELETE) ", output, re.MULTILINE)
        assert methods and methods[0] == expected, (argv, expected, output)
