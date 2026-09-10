import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    scripts_dir = repo_root / "scripts" / "util"
    sys.path.insert(0, str(scripts_dir))
    module_path = scripts_dir / "patch_identity.py"
    spec = importlib.util.spec_from_file_location("patch_identity", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return completed.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def test_patch_identity_git_ignores_hostile_path(
    patch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    marker = tmp_path / "fake-git-ran"
    fake_git = hostile / "git"
    fake_git.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 91\n", encoding="utf-8")
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(hostile))

    completed = module._git(patch_repo, "rev-parse", "HEAD")

    assert completed.returncode == 0
    assert not marker.exists()


def test_patch_identity_git_ignores_hostile_exec_path(
    patch_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = git(patch_repo, "rev-parse", "HEAD")
    (patch_repo / "shared.txt").write_text("changed\n", encoding="utf-8")
    candidate = commit_all(patch_repo, "candidate")
    hostile = tmp_path / "hostile-git-core"
    hostile.mkdir()
    marker = tmp_path / "fake-patch-id-ran"
    fake_patch_id = hostile / "git-patch-id"
    fake_patch_id.write_text(
        f"#!/bin/sh\ntouch '{marker}'\nprintf '%040d %040d\\n' 0 0\n",
        encoding="utf-8",
    )
    fake_patch_id.chmod(fake_patch_id.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("GIT_EXEC_PATH", str(hostile))

    identity = module.compute_patch_identity(
        patch_repo, base_sha=base, candidate_sha=candidate
    )

    assert identity["patch_id_verbatim"] != "0" * 40
    assert not marker.exists()


def test_patch_identity_git_scrubs_exec_path(
    patch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setenv("GIT_EXEC_PATH", "/attacker/git-core")
    monkeypatch.setenv("LD_PRELOAD", "/attacker/preload.so")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/attacker/inject.dylib")
    monkeypatch.setattr(module, "system_executable", lambda _name: Path("/usr/bin/git"))
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module._git(patch_repo, "status")

    environment = observed["env"]
    assert isinstance(environment, dict)
    assert "GIT_EXEC_PATH" not in environment
    assert "LD_PRELOAD" not in environment
    assert "DYLD_INSERT_LIBRARIES" not in environment


@pytest.fixture
def patch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "shared.txt").write_text(
        "upstream target\nkeep-1\nkeep-2\nkeep-3\nkeep-4\n"
        "author target\nkeep-5\nkeep-6\nkeep-7\nkeep-8\n",
        encoding="utf-8",
    )
    commit_all(repo, "base")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def test_rebase_and_nonconflicting_same_file_base_motion_replay_exactly(
    patch_repo: Path,
) -> None:
    repo = patch_repo
    base_one = git(repo, "rev-parse", "HEAD")
    (repo / "shared.txt").write_text(
        (repo / "shared.txt")
        .read_text(encoding="utf-8")
        .replace("author target", "author changed"),
        encoding="utf-8",
    )
    candidate_one = commit_all(repo, "author")
    left = module.compute_patch_identity(
        repo, base_sha=base_one, candidate_sha=candidate_one
    )

    git(repo, "checkout", "-q", "main")
    git(repo, "reset", "--hard", base_one)
    (repo / "shared.txt").write_text(
        (repo / "shared.txt")
        .read_text(encoding="utf-8")
        .replace("upstream target", "upstream changed"),
        encoding="utf-8",
    )
    base_two = commit_all(repo, "upstream same file")
    git(repo, "cherry-pick", candidate_one)
    candidate_two = git(repo, "rev-parse", "HEAD")
    right = module.compute_patch_identity(
        repo, base_sha=base_two, candidate_sha=candidate_two
    )

    receipt = module.prove_patch_equivalence(repo, left, right)

    assert receipt is not None
    assert receipt["result_tree_sha"] == right["candidate_tree_sha"]
    assert receipt["base_path_overlap"] == ["shared.txt"]
    assert receipt["replay_command"] == [
        "git",
        "apply",
        "--cached",
        "--3way",
        "--binary",
        "--whitespace=nowarn",
        "-",
    ]
    assert str(receipt["git_version"]).startswith("git version ")
    assert module.equivalence_receipt_is_valid(receipt)
    assert not module.equivalence_receipt_is_valid({**receipt, "return_code": 1})
    terminal = {
        "task_id": "review",
        "work_unit_id": "review",
        "run_id": "sr_" + "a" * 32,
        "snapshot_sha": candidate_one,
        "snapshot_tree_sha": left["candidate_tree_sha"],
        "patch_identity": left,
    }
    carry = module.build_patch_carry_record(
        terminal,
        current_source={"head": candidate_two},
        current_identity=right,
        replay_receipt=receipt,
    )
    assert carry["authority_scope"] == "model-review-only"
    assert "acceptance_receipt" not in carry
    assert module.validate_patch_carry(repo, terminal, carry) == (
        {"head": candidate_two},
        right,
        receipt,
    )
    assert (
        module.validate_patch_carry(
            repo, terminal, {**carry, "invalidated_receipts": []}
        )
        is None
    )
    assert module.patch_delta_churn(repo, left, right) == (0, receipt)


def test_repeated_block_patch_id_collision_does_not_grant_equivalence(
    patch_repo: Path,
) -> None:
    repo = patch_repo
    repeated = "head\na\nb\ntarget\nc\nd\ntail\n"
    (repo / "shared.txt").write_text(repeated + "gap\n" + repeated, encoding="utf-8")
    base = commit_all(repo, "repeated base")
    content = (repo / "shared.txt").read_text(encoding="utf-8")
    (repo / "shared.txt").write_text(
        content.replace("target", "changed", 1), encoding="utf-8"
    )
    first = commit_all(repo, "edit first block")
    left = module.compute_patch_identity(repo, base_sha=base, candidate_sha=first)

    git(repo, "reset", "--hard", base)
    content = (repo / "shared.txt").read_text(encoding="utf-8")
    first_offset = content.index("target")
    second_offset = content.index("target", first_offset + 1)
    (repo / "shared.txt").write_text(
        content[:second_offset] + "changed" + content[second_offset + len("target") :],
        encoding="utf-8",
    )
    second = commit_all(repo, "edit second block")
    right = module.compute_patch_identity(repo, base_sha=base, candidate_sha=second)

    assert left["patch_id_verbatim"] == right["patch_id_verbatim"]
    assert module.prove_patch_equivalence(repo, left, right) is None


def test_equivalence_rejects_a_regressed_or_forged_base(patch_repo: Path) -> None:
    repo = patch_repo
    original_base = git(repo, "rev-parse", "HEAD")
    (repo / "upstream.txt").write_text("trusted base motion\n", encoding="utf-8")
    reviewed_base = commit_all(repo, "reviewed base")
    (repo / "author.txt").write_text("author patch\n", encoding="utf-8")
    reviewed_head = commit_all(repo, "reviewed author patch")
    left = module.compute_patch_identity(
        repo, base_sha=reviewed_base, candidate_sha=reviewed_head
    )

    git(repo, "reset", "--hard", original_base)
    (repo / "author.txt").write_text("author patch\n", encoding="utf-8")
    regressed_head = commit_all(repo, "same patch on regressed base")
    right = module.compute_patch_identity(
        repo, base_sha=original_base, candidate_sha=regressed_head
    )

    assert left["patch_id_verbatim"] == right["patch_id_verbatim"]
    assert module.prove_patch_equivalence(repo, left, right) is None


def test_conflict_resolved_rebase_is_not_equivalent_and_owes_churn(
    patch_repo: Path,
) -> None:
    repo = patch_repo
    base = git(repo, "rev-parse", "HEAD")
    content = (repo / "shared.txt").read_text(encoding="utf-8")
    (repo / "shared.txt").write_text(
        content.replace("author target", "author version"), encoding="utf-8"
    )
    reviewed = commit_all(repo, "author patch")
    left = module.compute_patch_identity(repo, base_sha=base, candidate_sha=reviewed)

    git(repo, "reset", "--hard", base)
    (repo / "shared.txt").write_text(
        content.replace("author target", "upstream version"), encoding="utf-8"
    )
    rebased_base = commit_all(repo, "upstream conflict")
    cherry_pick = subprocess.run(
        ["git", "-C", str(repo), "cherry-pick", reviewed],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cherry_pick.returncode != 0
    (repo / "shared.txt").write_text(
        content.replace("author target", "resolved version"), encoding="utf-8"
    )
    git(repo, "add", "shared.txt")
    git(repo, "cherry-pick", "--continue", env={**os.environ, "GIT_EDITOR": "true"})
    resolved = git(repo, "rev-parse", "HEAD")
    right = module.compute_patch_identity(
        repo, base_sha=rebased_base, candidate_sha=resolved
    )

    assert module.prove_patch_equivalence(repo, left, right) is None
    delta = module.patch_delta_churn(repo, left, right)
    assert delta is not None
    assert delta[0] > 0


def test_conflict_edited_patch_owes_churn_and_large_edit_exceeds_cap(
    patch_repo: Path,
) -> None:
    repo = patch_repo
    base = git(repo, "rev-parse", "HEAD")
    (repo / "author.txt").write_text("one\ntwo\n", encoding="utf-8")
    first = commit_all(repo, "small patch")
    left = module.compute_patch_identity(repo, base_sha=base, candidate_sha=first)

    git(repo, "reset", "--hard", base)
    (repo / "author.txt").write_text(
        "".join(f"changed-{index}\n" for index in range(301)), encoding="utf-8"
    )
    second = commit_all(repo, "large conflict edit")
    right = module.compute_patch_identity(repo, base_sha=base, candidate_sha=second)

    delta = module.patch_delta_churn(repo, left, right)
    assert delta is None or delta[0] > 300


def test_append_only_repair_commits_owe_exact_cumulative_churn(
    patch_repo: Path,
) -> None:
    repo = patch_repo
    base = git(repo, "rev-parse", "HEAD")
    (repo / "author.txt").write_text("reviewed\n", encoding="utf-8")
    reviewed = commit_all(repo, "reviewed patch")
    left = module.compute_patch_identity(repo, base_sha=base, candidate_sha=reviewed)

    (repo / "author.txt").write_text("reviewed\nrepair\n", encoding="utf-8")
    commit_all(repo, "first repair")
    (repo / "author.txt").write_text("reviewed\nfixed\n", encoding="utf-8")
    repaired = commit_all(repo, "second repair")
    right = module.compute_patch_identity(repo, base_sha=base, candidate_sha=repaired)
    git(repo, "config", "diff.algorithm", "histogram")

    delta = module.patch_delta_churn(repo, left, right)

    assert delta == (3, None)


def test_append_only_merge_tail_stays_ambiguous(patch_repo: Path) -> None:
    repo = patch_repo
    base = git(repo, "rev-parse", "HEAD")
    (repo / "author.txt").write_text("reviewed\n", encoding="utf-8")
    reviewed = commit_all(repo, "reviewed patch")
    left = module.compute_patch_identity(repo, base_sha=base, candidate_sha=reviewed)

    git(repo, "checkout", "-qb", "repair-side")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    commit_all(repo, "side repair")
    git(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    commit_all(repo, "main repair")
    git(repo, "merge", "--no-ff", "-m", "merge repairs", "repair-side")
    merged = git(repo, "rev-parse", "HEAD")
    right = module.compute_patch_identity(repo, base_sha=base, candidate_sha=merged)

    assert module.patch_delta_churn(repo, left, right) is None


def test_append_only_timeout_stays_ambiguous(
    patch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = git(patch_repo, "rev-parse", "HEAD")
    (patch_repo / "author.txt").write_text("reviewed\n", encoding="utf-8")
    reviewed = commit_all(patch_repo, "reviewed patch")
    left = module.compute_patch_identity(
        patch_repo, base_sha=base, candidate_sha=reviewed
    )
    (patch_repo / "author.txt").write_text("repaired\n", encoding="utf-8")
    repaired = commit_all(patch_repo, "repair")
    right = module.compute_patch_identity(
        patch_repo, base_sha=base, candidate_sha=repaired
    )

    monkeypatch.setattr(
        module,
        "_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("git", 30)
        ),
    )

    assert module._append_only_churn(patch_repo, left, right) is None


def test_append_only_binary_repair_stays_ambiguous(patch_repo: Path) -> None:
    repo = patch_repo
    base = git(repo, "rev-parse", "HEAD")
    (repo / "author.txt").write_text("reviewed\n", encoding="utf-8")
    reviewed = commit_all(repo, "reviewed patch")
    left = module.compute_patch_identity(repo, base_sha=base, candidate_sha=reviewed)

    (repo / "binary.bin").write_bytes(bytes(range(256)))
    repaired = commit_all(repo, "binary repair")
    right = module.compute_patch_identity(repo, base_sha=base, candidate_sha=repaired)

    assert module.patch_delta_churn(repo, left, right) is None


def test_whitespace_patch_is_stable_and_large_rewrite_counts_or_fails_closed(
    patch_repo: Path,
) -> None:
    repo = patch_repo
    base = git(repo, "rev-parse", "HEAD")
    (repo / "rewrite.txt").write_text(
        "".join(f"old-{index}\n" for index in range(100)), encoding="utf-8"
    )
    original = commit_all(repo, "original series")
    original_identity = module.compute_patch_identity(
        repo, base_sha=base, candidate_sha=original
    )
    assert (
        module.prove_patch_equivalence(repo, original_identity, original_identity)
        is not None
    )

    (repo / "rewrite.txt").write_text(
        "".join(f"new-{index}  \n" for index in range(100)), encoding="utf-8"
    )
    rewritten = commit_all(repo, "rewrite with trailing whitespace")
    rewritten_identity = module.compute_patch_identity(
        repo, base_sha=base, candidate_sha=rewritten
    )

    delta = module.patch_delta_churn(repo, original_identity, rewritten_identity)
    assert delta is None or delta[0] >= 120


def test_raw_identity_handles_rename_mode_binary_gitlink_and_hostile_config(
    patch_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = patch_repo
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "mv", "shared.txt", "renamed.txt")
    mode_path = repo / "mode.sh"
    mode_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    mode_path.chmod(mode_path.stat().st_mode | stat.S_IXUSR)
    (repo / "binary.bin").write_bytes(bytes(range(256)))
    git(repo, "add", "-A")
    gitlink_target = git(repo, "rev-parse", "HEAD")
    git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{gitlink_target},vendor/sub",
    )
    git(repo, "commit", "-qm", "raw patch forms")
    candidate = git(repo, "rev-parse", "HEAD")
    baseline_identity = module.compute_patch_identity(
        repo, base_sha=base, candidate_sha=candidate
    )
    info_attributes = Path(
        git(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/attributes",
        )
    )

    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text(
        "[color]\n\tui = always\n[diff]\n\texternal = /definitely/missing\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.noprefix")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-git-dir"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "missing-objects"))
    identity = module.compute_patch_identity(
        repo, base_sha=base, candidate_sha=candidate
    )

    assert identity == baseline_identity
    assert identity["diff_format"] == module.PATCH_DIFF_FORMAT
    assert module.prove_patch_equivalence(repo, identity, identity) is not None

    info_attributes.parent.mkdir(parents=True, exist_ok=True)
    info_attributes.write_text("*.bin -diff\n", encoding="utf-8")
    assert module.capture_patch_identity(repo, candidate_sha=candidate) is None


@pytest.mark.parametrize("shape", ["rename", "mode", "binary", "gitlink"])
def test_individual_special_patch_shapes_are_stable_or_fail_closed(
    patch_repo: Path, shape: str
) -> None:
    repo = patch_repo
    if shape == "rename":
        git(repo, "mv", "shared.txt", "renamed.txt")
    elif shape == "mode":
        path = repo / "shared.txt"
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    elif shape == "binary":
        (repo / "binary.bin").write_bytes(bytes(range(256)))
    else:
        target = git(repo, "rev-parse", "HEAD")
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{target},vendor/sub")
    if shape == "gitlink":
        git(repo, "commit", "-qm", f"{shape} patch")
        candidate = git(repo, "rev-parse", "HEAD")
    else:
        candidate = commit_all(repo, f"{shape} patch")

    identity = module.capture_patch_identity(repo, candidate_sha=candidate)

    assert (
        identity is None
        or module.prove_patch_equivalence(repo, identity, identity) is not None
    )


def test_missing_or_malformed_identity_fails_closed(patch_repo: Path) -> None:
    repo = patch_repo
    assert (
        module.capture_patch_identity(
            repo, candidate_sha="HEAD", base_ref="refs/remotes/origin/missing"
        )
        is None
    )

    base = git(repo, "rev-parse", "HEAD")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    candidate = commit_all(repo, "candidate")
    identity = module.compute_patch_identity(
        repo, base_sha=base, candidate_sha=candidate
    )
    malformed = {**identity, "diff_sha256": "0" * 64}
    unrelated_base = git(
        repo,
        "commit-tree",
        str(identity["base_tree_sha"]),
        "-m",
        "unrelated base",
    )
    non_ancestor = module.compute_patch_identity(
        repo, base_sha=unrelated_base, candidate_sha=candidate
    )

    assert module.prove_patch_equivalence(repo, malformed, identity) is None
    assert module.prove_patch_equivalence(repo, non_ancestor, non_ancestor) is None


def test_range_diff_parser_counts_only_file_payload_and_rejects_ambiguity() -> None:
    output = b"""1:  aaaaaaa ! 1:  bbbbbbb subject
    @@ Commit message
    + rewritten metadata
    @@ file.txt:1,1 file.txt:1,1 @@
    +-old
    ++new
"""

    assert module.parse_range_diff_churn(output) == 2
    assert (
        module.parse_range_diff_churn(
            b"1:  aaaaaaa ! 1:  bbbbbbb subject\n"
            b"    @@ f\n"
            b"    +-old\n"
            b"    ++new\n"
        )
        == 2
    )
    assert module.parse_range_diff_churn(b" 1:  aaaaaaa =  1:  bbbbbbb same\n") == 0
    assert (
        module.parse_range_diff_churn(
            b" 1:  aaaaaaa !  1:  bbbbbbb changed\n"
            b"    @@ first.txt:1,1 first.txt:1,1 @@\n"
            b"    ++first\n"
            b"    +\n"
            b"    @@ second.txt:1,1 second.txt:1,1 @@\n"
            b"    ++second\n"
        )
        == 2
    )
    assert (
        module.parse_range_diff_churn(
            b"1:  aaaaaaa ! 1:  bbbbbbb changed\n"
            b"    @@ file.txt:1,1 file.txt:1,1 @@\n"
            b"    ++payload\n"
            b"    +\n"
        )
        == 1
    )
    assert module.parse_range_diff_churn(b"1: aaaaaaa < -: ------- removed\n") is None
    assert (
        module.parse_range_diff_churn(
            b"1:  aaaaaaa = 1:  bbbbbbb reviewed\n"
            b" -:  --------- > 9:  ccccccc added after reviewed series\n"
        )
        is None
    )
    assert (
        module.parse_range_diff_churn(
            b"1:  aaaaaaa ! 1:  bbbbbbb subject\n    +-outside-file\n"
        )
        is None
    )
    assert (
        module.parse_range_diff_churn(
            b"1:  aaaaaaa ! 1:  bbbbbbb subject\n"
            b"    @@ file.txt:1,1 file.txt:1,1 @@\n"
            b"    +\n"
        )
        is None
    )
    assert (
        module.parse_range_diff_churn(
            b"1:  aaaaaaa ! 1:  bbbbbbb subject\n"
            b"    @@ Unknown section\n"
            b"    +-old\n"
            b"    ++new\n"
        )
        is None
    )


@pytest.mark.parametrize(
    ("after", "allowed"),
    [
        ("def f():\n    return (1 + 2)\n", True),
        ("def f():\n    return 3\n", False),
        ("def f():\n    return 1+2  # noqa\n", False),
        ("def f():\n    pass\nreturn 1+2\n", False),
    ],
)
def test_python_format_carry_proves_syntax_and_comment_identity(
    patch_repo, after, allowed
):
    path = patch_repo / "format.py"
    path.write_text("def f():\n    return 1+2\n")
    before = commit_all(patch_repo, "before formatting")
    path.write_text(after)
    target = commit_all(patch_repo, "candidate")
    assert module.prove_format_only(patch_repo, before, target) is allowed
    path.chmod(0o755)
    mode_change = commit_all(patch_repo, "mode change")
    assert not module.prove_format_only(patch_repo, before, mode_change)


def test_mechanical_carry_retains_original_run_contract(patch_repo):
    import review_tree_coverage

    path = patch_repo / "format.py"
    path.write_text("VALUE=1\n")
    before = commit_all(patch_repo, "original")
    path.write_text("VALUE = 1\n")
    after = commit_all(patch_repo, "formatted")
    run_id = "sr_" + "a" * 32
    terminal = {
        "run_id": run_id,
        "snapshot_tree_sha": git(patch_repo, "rev-parse", before + "^{tree}"),
    }
    target = git(patch_repo, "rev-parse", after + "^{tree}")
    rows = patch_repo / ".audit/skill-runs"
    rows.mkdir(parents=True)
    record = rows / "2026-09-10.jsonl"
    import json

    record.write_text(json.dumps({"run_id": run_id}) + "\n")
    assert not review_tree_coverage.mechanical_review_carry(
        patch_repo, terminal, target
    )
    record.write_text(
        json.dumps({"run_id": run_id, "delivery_contract": "loop-zero-v1"}) + "\n"
    )
    assert review_tree_coverage.mechanical_review_carry(patch_repo, terminal, target)
    record.write_text("")
    assert not review_tree_coverage.mechanical_review_carry(
        patch_repo, terminal, target
    )


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("export const value = { a: 1 };\n", True),
        ("export const value = { a: 2 };\n", False),
    ],
)
def test_javascript_format_carry_uses_isolated_trusted_formatter(
    patch_repo, monkeypatch, candidate, expected
):
    import dispatch_common

    actual_primary = Path(__file__).resolve().parents[4]
    # Use the prepared repository toolchain; all source and Git refs stay in
    # the isolated test repository and the formatter receives stdin only.
    if not (
        actual_primary / "nextjs-frontend/node_modules/prettier/bin/prettier.cjs"
    ).is_file():
        pytest.skip("prepared frontend formatter is unavailable")
    monkeypatch.setattr(dispatch_common, "primary_repo_root", lambda _: actual_primary)
    path = patch_repo / "format.ts"
    path.write_text("export const value={a:1}\n")
    before = commit_all(patch_repo, "before format")
    path.write_text(candidate)
    after = commit_all(patch_repo, "after format")
    assert module.prove_format_only(patch_repo, before, after) is expected
