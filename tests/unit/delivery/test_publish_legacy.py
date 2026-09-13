import hashlib
import importlib
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def load_module() -> ModuleType:
    return importlib.import_module("loopzero.delivery.publish")


module = load_module()
publish_risk = importlib.import_module("loopzero.delivery._publish_risk")
from loopzero.kernel import authority as _authority
from loopzero.kernel import authority_projection as _projection
from loopzero.kernel import authority_store as _store
from loopzero.kernel import gitscope as _gitscope
from loopzero.kernel import policy as _policy
from loopzero.review import authority as _review_authority
from loopzero.review import chain as _chain

dispatch = SimpleNamespace(
    DISPATCH_POLICY_VERSION=_policy.DISPATCH_POLICY_VERSION,
    TELEMETRY_SCHEMA_VERSION=_policy.TELEMETRY_SCHEMA_VERSION,
    TerminalAuthority=_authority.TerminalAuthority,
    authenticated_review_terminals=_review_authority.authenticated_review_terminals,
    authenticated_supersessions=_projection.authenticated_supersessions,
    build_review_chain_receipt=_chain.build_review_chain_receipt,
    coordinator_ledger_prefix=_projection.coordinator_ledger_prefix,
    create_coordinator_authority=_store.create_coordinator_authority,
    review_terminal_acceptance_reasons=_review_authority.review_terminal_acceptance_reasons,
    task_contract_hash=_gitscope.task_contract_hash,
)
merge_gate = importlib.import_module("loopzero.delivery._publish_gate")
from loopzero.kernel import worktree_lease as worktree_guard


def context_body(trailer: str = "Closes #2645") -> str:
    return (
        "## Context and goal\n\n"
        "- **Context:** Teammates use pull requests to understand a change.\n"
        "- **Problem:** The purpose can otherwise be unclear.\n"
        "- **Goal:** Make the intended outcome easy to understand.\n\n"
        + trailer
        + "\n\n## Validation\n\n- Focused publication tests passed.\n"
    )


VALID_BODY = context_body()


def current_dispatch_record(record: dict[str, object]) -> dict[str, object]:
    """Give authority fixtures the current business-telemetry contract."""
    return {
        "schema_version": dispatch.TELEMETRY_SCHEMA_VERSION,
        "policy_version": dispatch.DISPATCH_POLICY_VERSION,
        **record,
    }


def current_dispatch_records(
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [current_dispatch_record(record) for record in records]


def coordinator_cutover(
    coordinator, *, prefix: list[dict[str, object]]
) -> dict[str, object]:
    """Activate the host coordinator against an exact fixture prefix."""
    return coordinator.seal(
        current_dispatch_record(
            {
                "type": "coordinator-authority-cutover",
                "status": "active",
                "ledger_prefix": dispatch.coordinator_ledger_prefix(prefix),
            }
        ),
        authority_kind="coordinator",
    )


@pytest.fixture(autouse=True)
def isolated_process_memory_contract(
    monkeypatch: pytest.MonkeyPatch, isolated_ptrace_scope_path: Path
) -> None:
    """Keep authority fixtures independent of the runner's Yama configuration."""
    authority_globals = (
        dispatch.TerminalAuthority.generate.__func__.__globals__,
        dispatch.create_coordinator_authority.__globals__[
            "CoordinatorAuthority"
        ].from_local_state.__func__.__globals__,
    )
    for globals_ in {id(item): item for item in authority_globals}.values():
        monkeypatch.setitem(globals_, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)


@pytest.fixture
def isolated_coordinator_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep signing and trust lookup local to this test, including in Guardian."""
    authority_globals = dispatch.create_coordinator_authority.__globals__[
        "CoordinatorAuthority"
    ].from_local_state.__func__.__globals__
    monkeypatch.setitem(
        authority_globals,
        "_coordinator_state_directory",
        lambda: tmp_path / "dispatch-authority",
    )
    # Use the real temporary private-key trust lookup instead of the host's
    # read-only Guardian projection. Signature verification remains unchanged.
    monkeypatch.setitem(
        authority_globals,
        "GUARDIAN_COORDINATOR_PUBLIC_KEY_PATH",
        tmp_path / "guardian-public-key.der",
    )


@pytest.mark.skip(reason="(a) consumer CLI front controller stays outside loopzero")
def test_script_entry_point_reaches_argument_parsing_without_pythonpath(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "util" / "pr_publish.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONSAFEPATH"] = "1"

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "the following arguments are required: --title" in completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr


class FakeRunner:
    def __init__(
        self,
        *,
        fail_labels: bool = False,
        remote_head: str = "a" * 40,
        returned_head: str | None = None,
        fresh_head: str | None = None,
        fresh_body: object = VALID_BODY,
        existing: list[dict[str, object]] | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, object] | None]] = []
        self.fail_labels = fail_labels
        self.remote_head = remote_head
        self.returned_head = returned_head or remote_head
        self.fresh_head = fresh_head or self.returned_head
        self.fresh_body = fresh_body
        self.existing = existing or []

    def run_json(
        self,
        args: list[str],
        *,
        payload: dict[str, object] | None = None,
    ) -> object:
        self.calls.append((args, payload))
        if "graphql" in args:
            return {
                "data": {
                    "resource": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        endpoint = args[-1]
        if "/branches/" in endpoint:
            return {"commit": {"sha": self.remote_head}}
        if endpoint == "repos/{owner}/{repo}":
            return {
                "full_name": "feder-positronics/intelflo",
                "owner": {"login": "feder-positronics"},
            }
        if endpoint == "repos/{owner}/{repo}/pulls" and "GET" in args:
            return self.existing
        if endpoint == "repos/{owner}/{repo}/pulls":
            return {
                "number": 2646,
                "html_url": "https://example.test/pull/2646",
                "head": {
                    "ref": "feature/2645-pr-publication",
                    "sha": self.returned_head,
                    "repo": {"full_name": "feder-positronics/intelflo"},
                },
                "base": {"ref": "main"},
            }
        if endpoint == "repos/{owner}/{repo}/pulls/2646" and "GET" in args:
            return {
                "number": 2646,
                "html_url": "https://example.test/pull/2646",
                "head": {
                    "ref": "feature/2645-pr-publication",
                    "sha": self.fresh_head,
                    "repo": {"full_name": "feder-positronics/intelflo"},
                },
                "base": {"ref": "main"},
                "body": self.fresh_body,
                "state": "open",
            }
        if endpoint == "repos/{owner}/{repo}/pulls/2646" and "PATCH" in args:
            return {"state": "closed"}
        if self.fail_labels:
            raise module.PublicationError("label mutation rejected")
        return {"names": ["in-progress", "ci"]}


class FakeGitRunner:
    def __init__(
        self,
        *,
        remote_head: str | None,
        tracking_head: str | None = None,
        divergent: bool = False,
        conflict_on_push: bool = False,
    ) -> None:
        self.remote_head = remote_head
        self.tracking_head = remote_head if tracking_head is None else tracking_head
        self.divergent = divergent
        self.conflict_on_push = conflict_on_push
        self.calls: list[tuple[list[str], bool, dict[str, str] | None]] = []

    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ):
        self.calls.append((args, check, env))
        if args[:3] == ["git", "check-ref-format", "--branch"]:
            return SimpleNamespace(returncode=0, stdout=args[-1] + "\n", stderr="")
        if args[:3] == ["git", "ls-remote", "--heads"]:
            stdout = f"{self.remote_head}\t{args[-1]}\n" if self.remote_head else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if args[:3] == ["git", "rev-parse", "--verify"]:
            return SimpleNamespace(
                returncode=0 if self.tracking_head else 1,
                stdout=f"{self.tracking_head}\n" if self.tracking_head else "",
                stderr="",
            )
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            return SimpleNamespace(
                returncode=1 if self.divergent else 0,
                stdout="",
                stderr="",
            )
        if args[:2] == ["git", "push"]:
            if self.conflict_on_push:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="stale info",
                )
            self.remote_head = args[-1].split(":", 1)[0]
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        raise AssertionError(f"unexpected Git command: {args}")


def test_trusted_publication_base_head_requires_exact_remote_tracking_ref() -> None:
    remote_head = "a" * 40

    assert (
        module.trusted_publication_base_head(
            FakeGitRunner(remote_head=remote_head), base="main"
        )
        == remote_head
    )

    with pytest.raises(module.PublicationError, match="tracking ref is stale"):
        module.trusted_publication_base_head(
            FakeGitRunner(remote_head=remote_head, tracking_head="b" * 40),
            base="main",
        )


def test_json_api_runner_reuses_the_pinned_repository_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "config",
            "remote.origin.url",
            "https://github.com/feder-positronics/intelflo.git",
        ],
        cwd=repo,
        check=True,
    )
    config = repo / ".git" / "config"
    observed: dict[str, object] = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        observed["input"] = kwargs["input"]
        return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setenv("GH_REPO", "attacker/other")
    monkeypatch.setattr(merge_gate.subprocess, "run", run)
    trusted_executable = merge_gate.system_executable
    monkeypatch.setattr(
        merge_gate,
        "system_executable",
        lambda binary: (
            Path("/usr/bin/gh") if binary == "gh" else trusted_executable(binary)
        ),
    )
    command_runner = module.SecureGitRunner(
        repo,
        origin_url="https://github.com/feder-positronics/intelflo.git",
        git_config_sha256=merge_gate.security_projection_sha256(config.read_bytes()),
    )
    runner = module.SubprocessRunner(command_runner)

    assert runner.run_json(
        ["gh", "api", "--method", "POST", "repos/{owner}/{repo}/pulls"],
        payload={"title": "bound"},
    ) == {"ok": True}

    assert Path(observed["command"][0]).name == "gh"
    assert observed["command"][1] == "api"
    assert observed["env"]["GH_REPO"] == "github.com/feder-positronics/intelflo"
    assert observed["input"] == '{"title": "bound"}'


def test_secure_publication_observations_ignore_replacement_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The outer acceptance sandbox correctly disables replacement objects.
    # This fixture must first create an enabled replacement to prove that the
    # runner's own environment, rather than its caller, disables observation.
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("reviewed\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "reviewed"], cwd=tmp_path, check=True)
    reviewed = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    reviewed_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=tmp_path, text=True
    ).strip()
    tracked.write_text("replacement\n")
    subprocess.run(["git", "commit", "-qam", "replacement"], cwd=tmp_path, check=True)
    replacement = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    replacement_tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=tmp_path, text=True
    ).strip()
    subprocess.run(["git", "checkout", "-q", reviewed], cwd=tmp_path, check=True)
    expected_identity = worktree_guard.source_identity(tmp_path)
    subprocess.run(["git", "replace", reviewed, replacement], cwd=tmp_path, check=True)

    assert (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=tmp_path, text=True
        ).strip()
        == replacement_tree
    )
    runner = module.SecureGitRunner(tmp_path)

    assert (
        runner.run(["git", "rev-parse", "HEAD^{tree}"]).stdout.strip() == reviewed_tree
    )
    assert module.secure_source_identity(runner) == expected_identity


def test_secure_publication_uses_pinned_primary_git_authority(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    attacker = tmp_path / "attacker"
    primary.mkdir()
    attacker.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=primary, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=primary, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=primary, check=True)
    subprocess.run(
        [
            "git",
            "config",
            "remote.origin.url",
            "https://github.com/feder-positronics/intelflo.git",
        ],
        cwd=primary,
        check=True,
    )
    (primary / "tracked.txt").write_text("trusted\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=primary, check=True)
    subprocess.run(["git", "commit", "-qm", "trusted"], cwd=primary, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-qb", "feature/pinned-authority", str(linked)],
        cwd=primary,
        check=True,
    )
    expected_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=linked, text=True
    ).strip()
    subprocess.run(["git", "init", "-q"], cwd=attacker, check=True)
    (linked / ".git").write_text(f"gitdir: {attacker / '.git'}\n", encoding="utf-8")
    config = primary / ".git" / "config"

    runner = module.SecureGitRunner(
        linked,
        origin_url="https://github.com/feder-positronics/intelflo.git",
        git_config_sha256=merge_gate.security_projection_sha256(config.read_bytes()),
        authority_repo=primary,
    )

    assert runner.run(["git", "rev-parse", "HEAD"]).stdout.strip() == expected_head
    assert module._repo_root(runner) == primary


def request(*, body: str = VALID_BODY, labels: tuple[str, ...] = ()):
    risk_json = '{"tier":"T1"}'
    return module.PublicationRequest(
        title="fix(agents): publish final PR metadata",
        body=body,
        head="feature/2645-pr-publication",
        base="main",
        labels=labels,
        expected_head="a" * 40,
        review_task_id="review-2645",
        review_source_identity={
            "version": 2,
            "ref": "refs/heads/feature/2645-pr-publication",
            "head": "a" * 40,
            "state_sha256": "state",
        },
        current_source_identity={
            "version": 2,
            "ref": "refs/heads/feature/2645-pr-publication",
            "head": "a" * 40,
            "state_sha256": "state",
        },
        finding_ids=("f_0123456789abcdefabcd",),
        known_finding_ids=("f_0123456789abcdefabcd",),
        review_risk_json=risk_json,
        review_risk_sha256=hashlib.sha256(risk_json.encode()).hexdigest(),
        review_risk_tier="T1",
        admission_tier_floor="T1",
    )


def test_t0_review_exemption_reuses_canonical_artifact_without_agent_policy(
    tmp_path: Path,
) -> None:
    docs_only = replace(
        request(),
        review_task_id="",
        review_source_identity=None,
        changed_paths=("docs/design/decision.md",),
        finding_ids=(),
        known_finding_ids=(),
        review_risk_json='{"tier":"T0"}',
        review_risk_sha256=hashlib.sha256(b'{"tier":"T0"}').hexdigest(),
        review_risk_tier="T0",
        admission_tier_floor="T0",
    )

    result = module.publish(FakeRunner(), docs_only, evidence_dir=tmp_path)

    assert result.number == 2646
    record = json.loads(next(tmp_path.glob("*.jsonl")).read_text().strip())
    assert record["review_task_id"] is None
    assert record["review_exemption"] == "T0"
    assert record["review_risk_sha256"] == docs_only.review_risk_sha256


def test_changed_paths_between_disables_rename_collapsing() -> None:
    class Runner:
        def run(self, args, **_kwargs):
            assert args == [
                "git",
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                "base..HEAD",
            ]
            return SimpleNamespace(
                stdout=".cursor/skills/review/SKILL.md\0docs/review.md\0"
            )

    paths = module.changed_paths_between(Runner(), "base", "HEAD")

    assert paths == (".cursor/skills/review/SKILL.md", "docs/review.md")


def test_changed_paths_between_preserves_literal_git_paths() -> None:
    class Runner:
        def run(self, _args, **_kwargs):
            return SimpleNamespace(stdout=" docs/naïve review.md \0")

    assert module.changed_paths_between(Runner(), "base", "HEAD") == (
        " docs/naïve review.md ",
    )


def test_t0_publication_requires_a_t0_risk_floor() -> None:
    without_mode_proof = replace(
        request(),
        review_task_id="",
        review_source_identity=None,
        changed_paths=("docs/design/decision.md",),
        finding_ids=(),
        known_finding_ids=(),
    )

    with pytest.raises(module.PublicationError, match="requires a final review"):
        module.validate_publication_request(without_mode_proof)


@pytest.mark.parametrize(
    "changed_paths",
    [
        ("scripts/util/pr_publish.py",),
        ("AGENTS.md",),
        (".cursor/skills/review/SKILL.md",),
    ],
)
def test_publication_without_review_fails_closed_outside_docs_exemption(
    changed_paths: tuple[str, ...],
) -> None:
    without_review = replace(
        request(),
        review_task_id="",
        review_source_identity=None,
        changed_paths=changed_paths,
    )

    with pytest.raises(module.PublicationError, match="requires a final review"):
        module.validate_publication_request(without_review)


def test_supplied_docs_review_still_requires_valid_review_identity() -> None:
    reviewed_docs = replace(
        request(),
        review_source_identity=None,
        changed_paths=("docs/design/decision.md",),
    )

    with pytest.raises(module.PublicationError, match="source identities are missing"):
        module.validate_publication_request(reviewed_docs)


def test_publish_warns_but_proceeds_without_visual_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advisory since PIL-VISUAL-DEMOTE-1: the missing-evidence signal is
    printed, publication is not blocked."""
    runner = FakeRunner()
    missing_evidence = replace(
        request(),
        changed_paths=("nextjs-frontend/components/repair-card.tsx",),
    )

    result = module.publish(runner, missing_evidence, evidence_dir=tmp_path)

    assert result.number == 2646
    assert "visual-evidence advisory" in capsys.readouterr().err


def test_publish_accepts_changed_playwright_journey_as_visual_evidence(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    valid = replace(
        request(),
        changed_paths=(
            "nextjs-frontend/components/repair-card.tsx",
            "nextjs-frontend/__tests__/critical/repair-card.spec.ts",
        ),
    )

    result = module.publish(runner, valid, evidence_dir=tmp_path)

    assert result.number == 2646


def test_publish_warns_but_admits_orphaned_finding_trailers_before_remote_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeRunner()
    orphaned = replace(
        request(),
        finding_ids=("f_fedcba9876543210abcd", "f_0123456789abcdefabcd"),
        known_finding_ids=("f_0123456789abcdefabcd",),
    )

    assert module.publish(runner, orphaned, evidence_dir=tmp_path).number == 2646

    assert capsys.readouterr().err == (
        "pr-publish: warning: exact Fixes trailers name Finding Ledger IDs absent "
        "from the canonical ledger: f_fedcba9876543210abcd. Deposit valid provenance "
        "before merge when readily available, or remove an incorrect trailer.\n"
    )
    assert runner.calls


def test_candidate_squash_trailer_collection_handles_split_duplicate_and_blank_paragraphs() -> (
    None
):
    commit_id = "f_0123456789abcdefabcd"
    body_id = "f_fedcba9876543210abcd"

    assert merge_gate.collect_exact_finding_ids(
        (
            f"fix: commit-only provenance for PR #3590\n\nFixes: {commit_id}\n\n"
            f"Fixes: {commit_id}",
            f"Closes #3576\n\nFixes: {body_id}",
        )
    ) == (commit_id, body_id)


def test_candidate_squash_trailer_inspection_reports_every_malformed_message() -> None:
    valid_id = "f_0123456789abcdefabcd"

    assert merge_gate.inspect_exact_finding_ids(
        (
            f"fix: valid\n\nFixes: {valid_id}",
            ("fix: malformed\n\nFixes: f_short\n" "Fixes: F_0123456789abcdefabcd"),
        )
    ) == (
        (valid_id,),
        (
            "malformed Finding Ledger trailer: 'Fixes: f_short'",
            "malformed Finding Ledger trailer: " "'Fixes: F_0123456789abcdefabcd'",
        ),
    )


def test_publish_is_quiet_for_deposited_issue_less_and_docs_only_admission(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deposited = replace(
        request(body=context_body("Closes #3607\n\nFixes: f_0123456789abcdefabcd")),
        changed_paths=("docs/guides/provenance.md",),
    )

    assert module.publish(FakeRunner(), deposited, evidence_dir=tmp_path).number == 2646
    assert capsys.readouterr().err == ""

    issue_less = replace(
        request(
            body=context_body(
                "Standalone-Reason: deliberately issue-less housekeeping"
            ),
            labels=("standalone",),
        ),
        finding_ids=(),
    )
    assert (
        module.publish(
            FakeRunner(fresh_body=issue_less.body),
            issue_less,
            evidence_dir=tmp_path,
        ).number
        == 2646
    )
    assert capsys.readouterr().err == ""


def test_publish_rejects_malformed_finding_trailer_before_remote_mutation() -> None:
    runner = FakeRunner()
    invalid = replace(
        request(),
        finding_provenance_errors=("Fixes: f_short",),
    )

    with pytest.raises(
        module.PublicationError, match="malformed Finding Ledger trailer"
    ):
        module.publish(runner, invalid)

    assert runner.calls == []


def test_publish_creates_final_pr_then_batches_all_labels_once(tmp_path: Path) -> None:
    runner = FakeRunner()

    result = module.publish(
        runner,
        request(labels=("in-progress", "ci", "in-progress")),
        evidence_dir=tmp_path,
    )

    assert result.number == 2646
    assert len(runner.calls) == 6
    assert runner.calls[3] == (
        [
            "gh",
            "api",
            "--method",
            "POST",
            "--input",
            "-",
            "repos/{owner}/{repo}/pulls",
        ],
        {
            "title": "fix(agents): publish final PR metadata",
            "body": VALID_BODY,
            "head": "feature/2645-pr-publication",
            "base": "main",
            "draft": False,
        },
    )
    assert runner.calls[5] == (
        [
            "gh",
            "api",
            "--method",
            "POST",
            "--input",
            "-",
            "repos/{owner}/{repo}/issues/2646/labels",
        ],
        {"labels": ["ci", "in-progress"]},
    )
    assert all("PATCH" not in args for args, _payload in runner.calls)

    records = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()
    ]
    assert records == [
        {
            "adopted": False,
            "base": "main",
            "body_repair_mutations": 0,
            "head": "feature/2645-pr-publication",
            "expected_head": "a" * 40,
            "admission_tier_floor": "T1",
            "review_risk_json": request().review_risk_json,
            "review_risk_sha256": request().review_risk_sha256,
            "review_risk_tier": "T1",
            "review_task_id": "review-2645",
            "run_id": None,
            "labels": ["ci", "in-progress"],
            "post_open_metadata_mutations": [
                {
                    "kind": "batch_labels",
                    "label_count": 2,
                    "status": "applied",
                    "unavoidable": True,
                }
            ],
            "pr": 2646,
            "status": "published",
            "url": "https://example.test/pull/2646",
        }
    ]


def test_publish_rejects_invalid_body_before_any_github_mutation(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    with pytest.raises(module.PublicationError, match="No work reference found"):
        module.publish(
            runner,
            request(body=context_body("No issue reference")),
            evidence_dir=tmp_path,
        )

    assert runner.calls == []


def test_publish_rejects_missing_plain_language_opening_before_github_mutation(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    with pytest.raises(module.PublicationError, match="Context and goal"):
        module.publish(
            runner,
            request(body="Closes #2645"),
            evidence_dir=tmp_path,
        )

    assert runner.calls == []


def test_publish_quarantines_new_pr_when_fresh_body_violates_policy(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(fresh_body="Closes #2645")

    with pytest.raises(module.PublicationError, match="live body violates"):
        module.publish(runner, request(), evidence_dir=tmp_path)

    assert any(
        args[-1] == "repos/{owner}/{repo}/pulls/2646"
        and "PATCH" in args
        and payload == {"state": "closed"}
        for args, payload in runner.calls
    )


def test_publish_rejects_incomplete_dispatch_lifecycle_before_github_mutation(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    with pytest.raises(module.PublicationError, match="dispatch lifecycle incomplete"):
        module.publish(
            runner,
            replace(
                request(),
                lifecycle_violations=(
                    "kept work unit docs lacks successful inline-complete",
                ),
            ),
            evidence_dir=tmp_path,
        )

    assert runner.calls == []


def test_publish_fails_closed_for_ready_standalone_pr_without_initial_reason(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()

    with pytest.raises(module.PublicationError, match="non-empty Standalone-Reason"):
        module.publish(
            runner,
            request(body="Deliberately issue-less", labels=("standalone",)),
            evidence_dir=tmp_path,
        )

    assert runner.calls == []


def test_publish_allows_ready_standalone_pr_with_initial_reason(
    tmp_path: Path,
) -> None:
    body = context_body("Standalone-Reason: recursive component audit")
    runner = FakeRunner(fresh_body=body)

    result = module.publish(
        runner,
        request(
            body=body,
            labels=("standalone",),
        ),
        evidence_dir=tmp_path,
    )

    assert result.number == 2646
    assert result.post_open_metadata_mutations == 1
    assert runner.calls[-1] == (
        [
            "gh",
            "api",
            "--method",
            "POST",
            "--input",
            "-",
            "repos/{owner}/{repo}/issues/2646/labels",
        ],
        {"labels": ["standalone"]},
    )


def test_publish_surfaces_open_pr_when_label_batch_fails(tmp_path: Path) -> None:
    runner = FakeRunner(fail_labels=True)

    with pytest.raises(
        module.PublicationError,
        match=r"PR #2646 opened, but its batched label mutation failed; evidence:",
    ):
        module.publish(
            runner,
            request(labels=("ci", "in-progress")),
            evidence_dir=tmp_path,
        )

    record = json.loads(next(tmp_path.glob("*.jsonl")).read_text().strip())
    assert record["status"] == "metadata_failed"
    assert record["post_open_metadata_mutations"] == [
        {
            "kind": "batch_labels",
            "label_count": 2,
            "status": "failed",
            "unavoidable": True,
        }
    ]


def test_repo_root_anchors_evidence_at_common_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_check_output(args: list[str], **_kwargs: object) -> str:
        calls.append(args)
        return "/repos/intelflo/.git\n"

    monkeypatch.setattr(module.subprocess, "check_output", fake_check_output)

    assert module._repo_root() == Path("/repos/intelflo")
    assert calls == [["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]]


def test_publication_marker_must_match_active_outer_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = "sr_" + "a" * 32
    monkeypatch.setattr(module, "active_outer_run_id", lambda worktree: run_id)

    module.require_active_publication_run(run_id=run_id, worktree=tmp_path)
    with pytest.raises(module.PublicationError, match="active outer run"):
        module.require_active_publication_run(
            run_id="sr_" + "b" * 32,
            worktree=tmp_path,
        )


def test_publish_rejects_local_or_remote_head_mismatch_before_creation(
    tmp_path: Path,
) -> None:
    local_mismatch = request()
    local_mismatch = replace(
        local_mismatch,
        current_source_identity={
            **local_mismatch.current_source_identity,
            "head": "b" * 40,
        },
    )
    local_runner = FakeRunner()

    with pytest.raises(module.PublicationError, match="local source"):
        module.publish(local_runner, local_mismatch, evidence_dir=tmp_path)
    assert local_runner.calls == []

    remote_runner = FakeRunner(remote_head="b" * 40)
    with pytest.raises(module.PublicationError, match="remote branch"):
        module.publish(remote_runner, request(), evidence_dir=tmp_path)
    assert not any(
        args[-1] == "repos/{owner}/{repo}/pulls" and "POST" in args
        for args, _payload in remote_runner.calls
    )


def test_stale_review_head_requires_exact_tree_delta_or_verified_patch_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = replace(
        request(),
        review_source_identity={
            "version": 2,
            "ref": "refs/heads/feature/2645-pr-publication",
            "head": "b" * 40,
            "state_sha256": "old-state",
        },
        review_snapshot_tree_sha="c" * 40,
        current_tree_sha="d" * 40,
    )

    with pytest.raises(module.PublicationError, match="exact reviewed tree"):
        module.validate_publication_request(stale)

    receipt = {"schema_version": "patch-equivalence-v1"}
    monkeypatch.setattr(
        module,
        "equivalence_receipt_is_valid",
        lambda candidate: candidate == receipt,
    )
    monkeypatch.setattr(
        module, "prove_patch_equivalence", lambda repo, left, right: receipt
    )
    covered = replace(
        stale,
        review_patch_identity={"candidate_sha": "b" * 40},
        current_patch_identity={"candidate_sha": "a" * 40},
        patch_equivalence=receipt,
    )
    assert module.verify_request_patch_equivalence(covered, tmp_path)
    module.validate_publication_request(covered, patch_identity_covers_head=True)
    runner = FakeRunner()
    with pytest.raises(module.PublicationError, match="exact reviewed tree"):
        module.publish(runner, covered, evidence_dir=tmp_path)
    assert runner.calls == []


def test_review_patch_identity_is_bound_to_terminal_source_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "review-bound"
    source_head = "a" * 40
    tree = "b" * 40
    identity = {"candidate_sha": source_head, "candidate_tree_sha": tree}
    terminal = {
        "task_id": task_id,
        "source_identity": {"head": source_head},
        "snapshot_sha": "c" * 40,
        "snapshot_tree_sha": tree,
        "patch_identity": identity,
    }
    monkeypatch.setattr(
        module,
        "latest_accepted_review_terminal",
        lambda records, accepted_task: records[-1]
        if records and accepted_task == task_id
        else None,
    )

    assert module.accepted_review_patch_identity([terminal], task_id) == identity
    synthetic_identity = {**identity, "candidate_sha": terminal["snapshot_sha"]}
    assert (
        module.accepted_review_patch_identity(
            [{**terminal, "patch_identity": synthetic_identity}], task_id
        )
        == synthetic_identity
    )
    assert (
        module.accepted_review_patch_identity(
            [{**terminal, "patch_identity": {**identity, "candidate_sha": "d" * 40}}],
            task_id,
        )
        is None
    )
    assert (
        module.accepted_review_patch_identity(
            [
                {
                    **terminal,
                    "patch_identity": {**identity, "candidate_tree_sha": "d" * 40},
                }
            ],
            task_id,
        )
        is None
    )


def test_publication_identity_uses_only_validated_captured_carry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    task_id = "review-carried"
    target_head = "c" * 40
    original = {"candidate_sha": "a" * 40, "candidate_tree_sha": "b" * 40}
    carried = {"candidate_sha": target_head, "candidate_tree_sha": "d" * 40}
    terminal = {
        "task_id": task_id,
        "source_identity": {"head": original["candidate_sha"]},
        "snapshot_tree_sha": original["candidate_tree_sha"],
        "patch_identity": original,
    }
    carry = {"task_id": task_id, "type": "attempt-patch-identity-carry"}
    monkeypatch.setattr(
        module,
        "latest_accepted_review_terminal",
        lambda records, accepted_task: records[0]
        if records and accepted_task == task_id
        else None,
    )
    monkeypatch.setattr(
        module,
        "validate_patch_carry",
        lambda repo, accepted, record: (
            ({"head": target_head}, carried, {"receipt": True})
            if accepted is terminal and record is carry
            else None
        ),
    )

    assert (
        module.carried_review_patch_identity(
            tmp_path, [terminal, carry], task_id, target_head
        )
        == carried
    )
    assert (
        module.carried_review_patch_identity(
            tmp_path, [terminal, carry], task_id, "e" * 40
        )
        is None
    )

    dirty_snapshot = {
        **terminal,
        "source_identity": {"head": target_head},
        "snapshot_sha": "f" * 40,
        "patch_identity": {
            **original,
            "candidate_sha": "f" * 40,
        },
    }
    assert (
        module.carried_review_patch_identity(
            tmp_path, [dirty_snapshot], task_id, target_head
        )
        is None
    )


@pytest.mark.parametrize(
    ("expected_head", "head", "branch", "status", "message"),
    [
        ("A" * 40, "a" * 40, "feature/2645-pr-publication", "", "40 lowercase"),
        ("a" * 40, "b" * 40, "feature/2645-pr-publication", "", "local HEAD"),
        (
            "b" * 40,
            "a" * 40,
            None,
            "",
            "HEAD is detached at " + "a" * 40 + "; run the publisher from "
            "the delivery worktree on branch feature/2645-pr-publication",
        ),
        (
            "a" * 40,
            "a" * 40,
            "other-branch",
            " M changed.py\n",
            "checked-out branch",
        ),
        (
            "a" * 40,
            "a" * 40,
            "feature/2645-pr-publication",
            " M changed.py\n",
            "clean worktree",
        ),
    ],
)
def test_local_publication_prerequisites_fail_before_authority_reads(
    tmp_path: Path,
    expected_head: str,
    head: str,
    branch: str | None,
    status: str,
    message: str,
) -> None:
    calls: list[list[str]] = []

    class PrerequisiteRunner:
        def run(self, args, **_kwargs):
            calls.append(args)
            if args[-2:] == ["rev-parse", "HEAD"]:
                return SimpleNamespace(returncode=0, stdout=head + "\n", stderr="")
            if "symbolic-ref" in args:
                return SimpleNamespace(
                    returncode=int(branch is None),
                    stdout=(branch or "") + "\n",
                    stderr="",
                )
            if args[-2:] == ["status", "--porcelain"]:
                return SimpleNamespace(returncode=0, stdout=status, stderr="")
            raise AssertionError(f"unexpected local prerequisite command: {args}")

    runner = PrerequisiteRunner()

    with pytest.raises(module.PublicationError, match=message):
        module.require_local_publication_prerequisites(
            worktree=tmp_path,
            head="feature/2645-pr-publication",
            expected_head=expected_head,
            runner=runner,
        )

    if expected_head != "a" * 40 and branch is not None:
        assert calls == []
    if branch == "other-branch":
        assert calls == [
            ["git", "rev-parse", "HEAD"],
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        ]


def test_local_publication_prerequisites_surface_git_boundary_cause(
    tmp_path: Path,
) -> None:
    """A pinned-binding failure names its cause instead of a generic message."""

    class BindingRunner:
        def run(self, args, **_kwargs):
            raise module.GateError(
                "pinned Git configuration changed before network access"
            )

    with pytest.raises(
        module.PublicationError,
        match="prerequisites: pinned Git configuration changed",
    ):
        module.require_local_publication_prerequisites(
            worktree=tmp_path,
            head="feature/2645-pr-publication",
            expected_head="a" * 40,
            runner=BindingRunner(),
        )


@pytest.mark.skip(reason="(a) consumer CLI front controller stays outside loopzero")
def test_main_rejects_local_prerequisite_before_loading_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unexpected_authority_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("authority readers must not run")

    monkeypatch.setattr(module, "_repo_root", unexpected_authority_read)

    with pytest.raises(SystemExit, match="2"):
        module.main(
            [
                "--title",
                "test",
                "--body-file",
                str(tmp_path / "not-read-yet.md"),
                "--head",
                "feature/2645-pr-publication",
                "--expected-head",
                "A" * 40,
                "--review-task-id",
                "review-2645",
            ]
        )


@pytest.mark.skip(reason="(a) consumer CLI front controller stays outside loopzero")
def test_main_rejects_partial_repository_binding_before_local_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unexpected_local_git(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("partial binding must fail before local Git reads")

    monkeypatch.setattr(module.subprocess, "check_output", unexpected_local_git)

    with pytest.raises(SystemExit, match="2"):
        module.main(
            [
                "--title",
                "test",
                "--body-file",
                str(tmp_path / "not-read.md"),
                "--head",
                "feature/2645-pr-publication",
                "--expected-head",
                "a" * 40,
                "--review-task-id",
                "review-2645",
                "--origin-url",
                "https://github.com/feder-positronics/intelflo.git",
            ]
        )


def test_publisher_source_must_come_from_the_pinned_primary_repository(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "primary" / "scripts" / "util" / "pr_publish.py"
    stale = tmp_path / "worktree" / "scripts" / "util" / "pr_publish.py"

    module.require_canonical_publisher_source(
        tmp_path / "primary", source_path=canonical
    )
    with pytest.raises(module.PublicationError, match="canonical primary"):
        module.require_canonical_publisher_source(
            tmp_path / "primary", source_path=stale
        )


@pytest.mark.parametrize("republish", [False, True])
@pytest.mark.parametrize("security_carry", [False, True, "uncovered", "foreign"])
@pytest.mark.parametrize("contract", ["intelflo-v1", "loop-zero-v1"])
@pytest.mark.skip(reason="(a) consumer CLI front controller stays outside loopzero")
def test_main_checks_obligations_and_emits_orphan_warning_before_convergence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    republish: bool,
    security_carry: bool | str,
    contract: str,
) -> None:
    expected_head = "a" * 40
    tree_sha = "b" * 40
    ownership = tmp_path / ".audit" / "skill-runs"
    ownership.mkdir(parents=True)
    (ownership / "2026-09-10.jsonl").write_text(
        json.dumps(
            {
                "run_id": "sr_" + "a" * 32,
                "delivery_contract": contract,
            }
        )
        + "\n"
    )
    head = "feature/3607-orphan-warning"
    source_identity = {
        "version": 2,
        "ref": f"refs/heads/{head}",
        "head": expected_head,
        "state_sha256": "state",
    }
    body_file = tmp_path / "body.md"
    body_file.write_text(
        context_body(
            "Closes #3607\n\nFixes: f_0123456789abcdefabcd\n\n"
            "<!-- skill-run-id: sr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->\n"
        ),
        encoding="utf-8",
    )

    class MainGitRunner:
        git_dir = tmp_path / ".git"

        def run(self, args, **_kwargs):
            if args == ["git", "rev-parse", "HEAD^{tree}"]:
                return SimpleNamespace(stdout=tree_sha + "\n")
            if args == ["git", "status", "--porcelain"]:
                return SimpleNamespace(stdout="")
            if args == ["git", "merge-base", "c" * 40, "HEAD"]:
                return SimpleNamespace(stdout="c" * 40 + "\n")
            if args[:3] == ["git", "diff", "--name-only"]:
                return SimpleNamespace(stdout="scripts/util/pr_publish.py\0")
            if args[:4] == ["git", "diff", "--name-status", "-M"]:
                return SimpleNamespace(stdout="")
            if args[:3] == ["git", "log", "--format=%B%x00"]:
                return SimpleNamespace(stdout="")
            raise AssertionError(args)

    git_runner = MainGitRunner()
    monkeypatch.setattr(module, "SecureGitRunner", lambda *_args, **_kwargs: git_runner)
    monkeypatch.setattr(
        module, "require_canonical_publisher_source", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        module, "require_local_publication_prerequisites", lambda **_kwargs: None
    )
    monkeypatch.setattr(module, "SubprocessRunner", lambda _runner: object())
    monkeypatch.setattr(module, "_repo_root", lambda _runner: tmp_path)
    publication_events: list[str] = []

    def preflight_adoption(_runner, _request, **_kwargs):
        publication_events.append("preflight-adoption")

    monkeypatch.setattr(
        module,
        "preflight_existing_adoption",
        preflight_adoption,
        raising=False,
    )
    monkeypatch.setattr(
        module, "require_active_publication_run", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        module, "worktree_lease", lambda *_args, **_kwargs: nullcontext()
    )
    monkeypatch.setattr(
        module,
        "trusted_publication_base_head",
        lambda *_args, **_kwargs: "c" * 40,
    )
    frozen_authority: list[dict[str, object]] = []
    authority_reads = 0

    def load_authority(_repo):
        nonlocal authority_reads
        authority_reads += 1
        assert authority_reads == 1
        return frozen_authority

    def lifecycle(rows, **_kwargs):
        assert rows is frozen_authority
        return []

    monkeypatch.setattr(module, "dispatch_lifecycle_violations", lifecycle)
    monkeypatch.setattr(
        module,
        "load_review_source_identity",
        lambda *_args, **kwargs: (
            (
                {
                    **source_identity,
                    "head": "f" * 40,
                    "ref": "refs/heads/foreign"
                    if security_carry == "foreign"
                    else source_identity["ref"],
                },
                "e" * 40,
                "security",
            )
            if security_carry and kwargs.get("expected_lens") == "security"
            else (source_identity, tree_sha, "code")
        ),
    )
    if security_carry:
        monkeypatch.setattr(
            module, "accepted_review_patch_identity", lambda *_a: {"review": True}
        )

        def carried_identity(_repo, _records, _task, target, *, current_source):
            assert current_source == source_identity
            assert target == expected_head
            return {"current": True}

        monkeypatch.setattr(module, "carried_review_patch_identity", carried_identity)
        monkeypatch.setattr(
            module, "prove_patch_equivalence", lambda *_a: {"proof": True}
        )

        def security_equivalence(_repo, reviewed, current, *, lens, runner):
            assert reviewed == {"review": True}
            assert current == {"current": True}
            assert lens == "security"
            assert runner is git_runner
            return None if security_carry == "uncovered" else {"proof": True}

        monkeypatch.setattr(
            module.review_tree_coverage,
            "section_patch_equivalence",
            security_equivalence,
        )
    risk_json = '{"tier":"T1"}'
    monkeypatch.setattr(
        module,
        "review_risk_envelope_for_publication",
        lambda *_args, **_kwargs: (
            risk_json,
            hashlib.sha256(risk_json.encode()).hexdigest(),
        ),
    )
    monkeypatch.setattr(
        module.delivery_review_risk,
        "verify_review_risk",
        lambda *_args, **_kwargs: {
            "artifact": {
                "base_sha": "c" * 40,
                "changed_paths": ["scripts/util/pr_publish.py"],
                "head_sha": expected_head,
                "head_tree_sha": tree_sha,
                "security_trigger_paths": [],
                "tier": "T1",
            },
            "effective_security_trigger_paths": ["scripts/util/pr_publish.py"]
            if security_carry
            else [],
            "effective_tier": "T1",
        },
    )
    monkeypatch.setattr(
        module, "secure_source_identity", lambda _runner: source_identity
    )
    monkeypatch.setattr(module, "_all_dispatch_records", load_authority)
    monkeypatch.setattr(module, "load_finding_records", lambda *_a, **_k: [])
    monkeypatch.setattr(module, "load_delta_edges", lambda *_a, **_k: {})
    monkeypatch.setattr(module, "open_important_finding_ids", lambda *_a, **_k: ())

    converged: list[str] = []

    def converge(
        _runner, *, head, expected_head, repo, base_head, base, equivalence_evidence
    ):
        assert repo == Path.cwd()
        assert base == "main"
        assert base_head
        assert equivalence_evidence == {}
        if republish:
            equivalence_evidence.update({"observed_head": "c" * 40})
        assert publication_events == ["obligation-check", "preflight-adoption"]
        publication_events.append("converge")
        assert capsys.readouterr().err == (
            "pr-publish: warning: exact Fixes trailers name Finding Ledger IDs "
            "absent from the canonical ledger: f_0123456789abcdefabcd. Deposit "
            "valid provenance before merge when readily available, or remove an "
            "incorrect trailer.\n"
        )
        converged.append(f"{head}:{expected_head}")

    monkeypatch.setattr(module, "converge_remote_publication_head", converge)
    written_evidence = []
    monkeypatch.setattr(
        module, "_write_evidence", lambda directory, row: written_evidence.append(row)
    )
    monkeypatch.setattr(
        module,
        "require_remote_publication_prerequisites",
        lambda *_args, **_kwargs: {"sha": expected_head},
    )

    def publish(_runner, request, **kwargs):
        assert request.finding_ids == ("f_0123456789abcdefabcd",)
        assert kwargs["warning_emitted"] is True
        assert ("loop-zero-evidence:start" in request.body) == (
            contract == "loop-zero-v1"
        )
        return module.PublicationResult(
            number=3608,
            url="https://example.test/pull/3608",
            evidence_path=tmp_path / "evidence.jsonl",
            post_open_metadata_mutations=0,
        )

    monkeypatch.setattr(module, "publish", publish)
    monkeypatch.setattr(
        module, "validate_body_against_trusted_base", lambda *_args, **_kwargs: None
    )

    monkeypatch.setattr(
        module,
        "require_obligation_acknowledgment",
        lambda *_args: publication_events.append("obligation-check"),
    )

    args = [
        "--title",
        "fix(ops): warn on orphan finding trailers",
        "--body-file",
        str(body_file),
        "--head",
        head,
        "--expected-head",
        expected_head,
        "--review-task-id",
        "review-3607",
        "--origin-url",
        "https://github.com/feder-positronics/intelflo.git",
        "--git-config-sha256",
        "d" * 64,
        "--authority-repo",
        str(tmp_path),
    ]
    if security_carry in {"uncovered", "foreign"}:
        with pytest.raises(SystemExit):
            module.main(args)
        error = capsys.readouterr().err
        assert (
            "different branch"
            if security_carry == "foreign"
            else "security review does not cover"
        ) in error
        assert converged == []
        assert written_evidence == []
        assert publication_events == ["obligation-check"]
        return
    assert module.main(args) == 0
    assert converged == [f"{head}:{expected_head}"]
    assert publication_events == ["obligation-check", "preflight-adoption", "converge"]
    assert authority_reads == 1
    assert len(written_evidence) == int(republish)
    if republish:
        assert written_evidence[0]["status"] == "remote-republished"
        assert written_evidence[0]["republish_equivalence"] == {
            "observed_head": "c" * 40
        }


def test_remote_convergence_creates_missing_branch_with_empty_lease() -> None:
    runner = FakeGitRunner(remote_head=None)

    remote = module.converge_remote_publication_head(
        runner,
        head="feature/2645-pr-publication",
        expected_head="a" * 40,
    )

    assert remote == "a" * 40
    push = next(
        args for args, _check, _env in runner.calls if args[:2] == ["git", "push"]
    )
    assert "--force-with-lease=refs/heads/feature/2645-pr-publication:" in push


def test_remote_convergence_reuses_exact_head_without_push() -> None:
    runner = FakeGitRunner(remote_head="a" * 40)

    remote = module.converge_remote_publication_head(
        runner,
        head="feature/2645-pr-publication",
        expected_head="a" * 40,
    )

    assert remote == "a" * 40
    assert not any(args[:2] == ["git", "push"] for args, _check, _env in runner.calls)


def test_remote_convergence_fast_forwards_observed_head_and_verifies() -> None:
    runner = FakeGitRunner(remote_head="b" * 40)

    remote = module.converge_remote_publication_head(
        runner,
        head="feature/2645-pr-publication",
        expected_head="a" * 40,
    )

    assert remote == "a" * 40
    push = next(
        args for args, _check, _env in runner.calls if args[:2] == ["git", "push"]
    )
    assert (
        "--force-with-lease=refs/heads/feature/2645-pr-publication:" + "b" * 40 in push
    )
    assert (
        len(
            [
                args
                for args, _check, _env in runner.calls
                if args[:3] == ["git", "ls-remote", "--heads"]
            ]
        )
        == 2
    )


def test_remote_convergence_rejects_divergence_without_push() -> None:
    runner = FakeGitRunner(remote_head="b" * 40, divergent=True)

    with pytest.raises(module.PublicationError, match="exact reviewed tree"):
        module.converge_remote_publication_head(
            runner,
            head="feature/2645-pr-publication",
            expected_head="a" * 40,
        )

    assert not any(args[:2] == ["git", "push"] for args, _check, _env in runner.calls)


def test_remote_convergence_rejects_concurrent_remote_movement() -> None:
    runner = FakeGitRunner(remote_head="b" * 40, conflict_on_push=True)

    with pytest.raises(module.PublicationError, match="changed during publication"):
        module.converge_remote_publication_head(
            runner,
            head="feature/2645-pr-publication",
            expected_head="a" * 40,
        )


@pytest.mark.parametrize("head", ["main", "master", "refs/heads/main"])
def test_remote_convergence_never_updates_protected_branch(head: str) -> None:
    runner = FakeGitRunner(remote_head="b" * 40)
    with pytest.raises(module.PublicationError, match="protected"):
        module.converge_remote_publication_head(
            runner, head=head, expected_head="a" * 40
        )
    assert not any(args[:2] == ["git", "push"] for args, _, _ in runner.calls)
    assert not any(args[:2] == ["git", "ls-remote"] for args, _, _ in runner.calls)


@pytest.mark.parametrize("concurrent", [False, True])
def test_remote_convergence_equivalent_rebase_uses_observed_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, concurrent: bool
) -> None:
    runner = FakeGitRunner(
        remote_head="b" * 40, divergent=True, conflict_on_push=concurrent
    )
    proof = {"observed_head": "b" * 40, "expected_head": "a" * 40}
    monkeypatch.setattr(
            importlib.import_module("loopzero.delivery._publish_remote"),
        "prove_republish_equivalence",
        lambda **kwargs: proof,
    )
    evidence: dict[str, object] = {}
    kwargs = dict(
        head="feature/2645-pr-publication",
        expected_head="a" * 40,
        repo=tmp_path,
        base_head="c" * 40,
        equivalence_evidence=evidence,
    )
    if concurrent:
        with pytest.raises(module.PublicationError, match="changed during publication"):
            module.converge_remote_publication_head(runner, **kwargs)
        assert evidence == {}
    else:
        assert module.converge_remote_publication_head(runner, **kwargs) == "a" * 40
        assert evidence == proof
    push = next(args for args, _, _ in runner.calls if args[:2] == ["git", "push"])
    assert (
        "--force-with-lease=refs/heads/feature/2645-pr-publication:" + "b" * 40 in push
    )
    assert push[-1] == "a" * 40 + ":refs/heads/feature/2645-pr-publication"
    assert "--force" not in push


def test_publish_reuses_prevalidated_remote_branch(tmp_path: Path) -> None:
    runner = FakeRunner()

    result = module.publish(
        runner,
        request(),
        evidence_dir=tmp_path,
        remote_branch={"commit": {"sha": "a" * 40}},
    )

    assert result.number == 2646
    assert not any("/branches/" in args[-1] for args, _payload in runner.calls)


def test_publish_accepts_snapshot_tree_match_over_identity_mismatch(
    tmp_path: Path,
) -> None:
    """FL-1 bridge (#3106 A2 L1): a snapshot-bound review is valid when its
    snapshot tree equals the clean publication head tree, even though the
    live identity drifted after the review."""
    snapshot_bound = replace(
        request(),
        current_source_identity={
            "version": 2,
            "ref": "refs/heads/feature/2645-pr-publication",
            "head": "a" * 40,
            "state_sha256": "drifted-after-review",
        },
        review_snapshot_tree_sha="tree" * 10,
        current_tree_sha="tree" * 10,
    )

    result = module.publish(FakeRunner(), snapshot_bound, evidence_dir=tmp_path)

    assert result.number == 2646

    tree_mismatch = replace(snapshot_bound, current_tree_sha="othertree" + "0" * 31)
    with pytest.raises(module.PublicationError, match="local source"):
        module.publish(FakeRunner(), tree_mismatch, evidence_dir=tmp_path)


def test_chain_covers_tree_walks_delta_edges() -> None:
    edges = {"t0": "t1", "t1": "t2"}
    assert module.chain_covers_tree("t0", edges, "t2")
    assert module.chain_covers_tree("t2", edges, "t2")
    assert not module.chain_covers_tree("t0", edges, "t9")
    assert not module.chain_covers_tree(None, edges, "t2")
    # Cycles terminate at the hop cap instead of hanging.
    assert not module.chain_covers_tree("a", {"a": "b", "b": "a"}, "z")


def test_publication_review_coverage_predicate_fails_closed_for_stale_tree() -> None:
    reviewed_source = {
        "version": 2,
        "ref": "refs/heads/fix/publication",
        "head": "a" * 40,
        "state_sha256": "b" * 64,
    }
    current_source = {
        **reviewed_source,
        "head": "c" * 40,
        "state_sha256": "d" * 64,
    }

    assert not module.review_tree_coverage.covers_frozen_tree(
        review_snapshot_tree="e" * 40,
        current_tree="f" * 40,
        chain_covered=False,
        review_source=reviewed_source,
        current_source=current_source,
        patch_equivalence=None,
    )
    assert module.review_tree_coverage.covers_frozen_tree(
        review_snapshot_tree="e" * 40,
        current_tree="f" * 40,
        chain_covered=True,
        review_source=reviewed_source,
        current_source=current_source,
        patch_equivalence=None,
    )


def test_publication_review_task_coverage_accepts_passing_exact_security_tree(
    tmp_path: Path,
) -> None:
    task_id = "exact-security-review"
    tree = "e" * 40
    source = {
        "version": 2,
        "ref": "refs/heads/fix/publication",
        "head": "a" * 40,
        "state_sha256": "b" * 64,
    }
    terminal = {
        "type": "attempt-terminal",
        "task_id": task_id,
        "status": "completed",
        "review_intent": "delivery-code-review",
        "source_identity": source,
        "snapshot_tree_sha": tree,
        "review_chain_receipt": {
            "required_sections": ["code", "security"],
            "sections": {"code": {}, "security": {}},
        },
        "task_contract": {"review_intent": "delivery-code-review"},
    }

    assert module.review_tree_coverage.review_task_covers_tree(
        tmp_path,
        records=[terminal],
        accepted_terminals={task_id: terminal},
        latest_verdicts={task_id: {"verdict": "pass"}},
        finding_records=[],
        task_id=task_id,
        lens="security",
        current_source=source,
        current_tree=tree,
    )


def test_publication_review_task_coverage_rejects_legacy_source_identity(
    tmp_path: Path,
) -> None:
    task_id = "legacy-security-review"
    tree = "e" * 40
    source = {
        "version": 1,
        "ref": "refs/heads/fix/publication",
        "head": "a" * 40,
        "state_sha256": "b" * 64,
    }
    terminal = {
        "type": "attempt-terminal",
        "task_id": task_id,
        "status": "completed",
        "review_intent": "delivery-code-review",
        "source_identity": source,
        "snapshot_tree_sha": tree,
        "review_chain_receipt": {
            "required_sections": ["code", "security"],
            "sections": {"code": {}, "security": {}},
        },
        "task_contract": {"review_intent": "delivery-code-review"},
    }

    assert not module.review_tree_coverage.review_task_covers_tree(
        tmp_path,
        records=[terminal],
        accepted_terminals={task_id: terminal},
        latest_verdicts={task_id: {"verdict": "pass"}},
        finding_records=[],
        task_id=task_id,
        lens="security",
        current_source=source,
        current_tree=tree,
    )


def test_load_delta_edges_projects_accepted_review_terminals_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    branch = "refs/heads/feature/reviewed"
    category = "pr-publication-evidence"
    identity = {
        "version": 2,
        "ref": branch,
        "head": "a" * 40,
        "state_sha256": "state",
    }
    records = current_dispatch_records(
        [
            {
                "type": "attempt-terminal",
                "task_id": "receiptless-delta",
                "status": "completed",
                "read_only": True,
                "work_kind": "review",
                "review_intent": "delivery-code-review",
                "review_lens": category,
                "task_contract": {
                    "review_intent": "delivery-code-review",
                    "review_lens": category,
                },
                "category": category,
                "source_identity": identity,
                "delta_from_tree_sha": "tree0",
                "snapshot_tree_sha": "tree1",
            },
            {
                "type": "attempt-terminal",
                "task_id": "accepted-delta",
                "status": "completed",
                "read_only": True,
                "work_kind": "review",
                "review_intent": "delivery-code-review",
                "review_lens": category,
                "task_contract": {
                    "review_intent": "delivery-code-review",
                    "review_lens": category,
                },
                "category": category,
                "source_identity": identity,
                "delta_from_tree_sha": "tree1",
                "snapshot_tree_sha": "tree2",
            },
            {"type": "verdict", "task_id": "accepted-delta", "verdict": "pass"},
        ]
    )
    projection_calls = 0
    monkeypatch.setattr(module, "_all_dispatch_records", lambda repo: records)

    def accepted(rows):
        nonlocal projection_calls
        projection_calls += 1
        assert rows is records
        return {"accepted-delta": records[1]}

    monkeypatch.setattr(module, "accepted_review_terminals", accepted)
    monkeypatch.setattr(
        module,
        "latest_accepted_review_terminal",
        lambda *_args, **_kwargs: pytest.fail("per-task terminal projection"),
    )

    assert module.load_delta_edges(tmp_path, lens=category, source_ref=branch) == {
        "tree1": "tree2"
    }
    assert projection_calls == 1


def test_delta_and_blocker_evaluation_reuse_one_authority_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    branch = "refs/heads/feature/reviewed"
    terminal = {
        "type": "attempt-terminal",
        "task_id": "accepted-delta",
        "status": "completed",
        "read_only": True,
        "work_kind": "review",
        "review_intent": "delivery-code-review",
        "review_lens": "code",
        "task_contract": {
            "review_intent": "delivery-code-review",
            "review_lens": "code",
        },
        "source_identity": {"ref": branch},
        "delta_from_tree_sha": "tree0",
        "snapshot_tree_sha": "tree1",
    }
    dispatch_records = current_dispatch_records(
        [
            terminal,
            {"type": "verdict", "task_id": "accepted-delta", "verdict": "pass"},
        ]
    )
    terminal = dispatch_records[0]
    finding_records = [
        {
            "finding_id": "f_block",
            "state": "open",
            "severity": "important",
            "review_task_id": "accepted-delta",
        }
    ]
    monkeypatch.setattr(
        module,
        "_all_dispatch_records",
        lambda repo: pytest.fail("dispatch authority was reloaded"),
    )
    monkeypatch.setattr(
        module,
        "load_finding_records",
        lambda repo, **kwargs: pytest.fail("finding authority was reloaded"),
    )
    monkeypatch.setattr(
        module,
        "accepted_review_terminals",
        lambda rows: {"accepted-delta": terminal},
    )

    assert module.load_delta_edges(
        tmp_path,
        lens="code",
        source_ref=branch,
        dispatch_records=dispatch_records,
        finding_records=finding_records,
    ) == {"tree0": "tree1"}
    assert module.open_important_finding_ids(
        tmp_path,
        branch,
        dispatch_records=dispatch_records,
        finding_records=finding_records,
    ) == ("f_block",)


def test_load_delta_edges_rejects_cross_lens_ref_and_verdict_mismatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Publication may only walk same-category, same-branch, pass-verdicted edges."""
    branch = "refs/heads/feature/reviewed"
    category = "pr-publication-evidence"
    base_identity = {
        "version": 2,
        "ref": branch,
        "head": "a" * 40,
        "state_sha256": "state",
    }

    def delta_terminal(
        task_id: str,
        *,
        from_tree: str,
        to_tree: str,
        cat: str = category,
        ref: str = branch,
    ) -> dict[str, object]:
        return {
            "type": "attempt-terminal",
            "task_id": task_id,
            "status": "completed",
            "read_only": True,
            "work_kind": "review",
            "review_intent": "delivery-code-review",
            "review_lens": cat,
            "task_contract": {
                "review_intent": "delivery-code-review",
                "review_lens": cat,
            },
            "category": cat,
            "source_identity": {**base_identity, "ref": ref},
            "delta_from_tree_sha": from_tree,
            "snapshot_tree_sha": to_tree,
        }

    records = current_dispatch_records(
        [
            delta_terminal(
                "wrong-category", from_tree="t0", to_tree="t1", cat="other-lens"
            ),
            {"type": "verdict", "task_id": "wrong-category", "verdict": "pass"},
            delta_terminal(
                "wrong-branch",
                from_tree="t0",
                to_tree="t1",
                ref="refs/heads/feature/other",
            ),
            {"type": "verdict", "task_id": "wrong-branch", "verdict": "pass"},
            delta_terminal("receiptless", from_tree="t0", to_tree="t1"),
            {"type": "verdict", "task_id": "receiptless", "verdict": "pass"},
            delta_terminal("missing-verdict", from_tree="t0", to_tree="t1"),
            delta_terminal("failing-verdict", from_tree="t0", to_tree="t1"),
            {"type": "verdict", "task_id": "failing-verdict", "verdict": "fail"},
            delta_terminal("good-edge", from_tree="t1", to_tree="t2"),
            {"type": "verdict", "task_id": "good-edge", "verdict": "pass"},
        ]
    )
    monkeypatch.setattr(module, "_all_dispatch_records", lambda repo: records)

    monkeypatch.setattr(
        module,
        "accepted_review_terminals",
        lambda rows: {
            str(row["task_id"]): row
            for row in rows
            if row.get("type") == "attempt-terminal"
            and row.get("task_id") != "receiptless"
        },
    )

    assert module.load_delta_edges(tmp_path, lens=category, source_ref=branch) == {
        "t1": "t2"
    }
    assert module.chain_covers_tree("t1", {"t1": "t2"}, "t2")
    assert not module.chain_covers_tree(
        "t0",
        module.load_delta_edges(tmp_path, lens=category, source_ref=branch),
        "t2",
    )


def test_load_delta_edges_uses_hash_bound_delivery_lens_not_category(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    branch = "refs/heads/feature/reviewed"

    def terminal(task_id: str, *, lens: str, category: str, start: str, end: str):
        return {
            "type": "attempt-terminal",
            "task_id": task_id,
            "status": "completed",
            "read_only": True,
            "work_kind": "review",
            "review_intent": "delivery-code-review",
            "review_lens": lens,
            "category": category,
            "source_identity": {"ref": branch},
            "delta_from_tree_sha": start,
            "snapshot_tree_sha": end,
            "task_contract": {
                "review_intent": "delivery-code-review",
                "review_lens": lens,
                "category": category,
            },
        }

    code_edge = terminal(
        "code-edge", lens="code", category="renamed-label", start="t0", end="t1"
    )
    security_edge = terminal(
        "security-edge",
        lens="security",
        category="original-label",
        start="t1",
        end="t2",
    )
    records = current_dispatch_records(
        [
            code_edge,
            {"type": "verdict", "task_id": "code-edge", "verdict": "pass"},
            security_edge,
            {"type": "verdict", "task_id": "security-edge", "verdict": "pass"},
        ]
    )
    code_edge, security_edge = records[0], records[2]
    monkeypatch.setattr(module, "_all_dispatch_records", lambda repo: records)
    monkeypatch.setattr(
        module,
        "accepted_review_terminals",
        lambda rows: {"code-edge": code_edge, "security-edge": security_edge},
    )

    assert module.load_delta_edges(tmp_path, lens="code", source_ref=branch) == {
        "t0": "t1"
    }


@pytest.mark.parametrize("security_paths", ((), ("scripts/util/agent_dispatch.py",)))
def test_code_review_delta_advances_security_only_without_a_new_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    security_paths: tuple[str, ...],
) -> None:
    branch = "refs/heads/feature/reviewed"
    source_tree = "1" * 40
    target_tree = "2" * 40
    terminal = {
        "type": "attempt-terminal",
        "task_id": "code-delta",
        "status": "completed",
        "read_only": True,
        "work_kind": "review",
        "review_intent": "delivery-code-review",
        "source_identity": {"ref": branch},
        "delta_from_tree_sha": source_tree,
        "snapshot_tree_sha": target_tree,
        "task_contract": {"review_intent": "delivery-code-review"},
        "review_chain_receipt": {
            "required_sections": ["code"],
            "sections": {"code": {"completion": "completed", "verdict": "clean"}},
        },
    }
    records = current_dispatch_records(
        [
            terminal,
            {"type": "verdict", "task_id": "code-delta", "verdict": "pass"},
        ]
    )
    terminal = records[0]
    monkeypatch.setattr(module, "_all_dispatch_records", lambda repo: records)
    monkeypatch.setattr(
        module,
        "accepted_review_terminals",
        lambda rows: {"code-delta": terminal},
    )
    monkeypatch.setattr(
        module,
        "_security_trigger_paths_between",
        lambda *args, **kwargs: security_paths,
    )

    edges = module.load_delta_edges(tmp_path, lens="security", source_ref=branch)

    assert edges == ({} if security_paths else {source_tree: target_tree})


def test_deterministic_finding_evidence_extends_review_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".audit" / "findings").mkdir(parents=True)
    branch = "refs/heads/feature/reviewed"
    source_tree = "1" * 40
    target_tree = "2" * 40
    contract = {"review_intent": "delivery-code-review", "review_lens": "code"}
    terminal = {
        "type": "attempt-terminal",
        "task_id": "fixed-review",
        "status": "completed",
        "read_only": True,
        "work_kind": "review",
        **contract,
        "task_contract": contract,
        "source_identity": {"ref": branch},
        "snapshot_tree_sha": source_tree,
    }
    records = current_dispatch_records(
        [
            terminal,
            {"type": "verdict", "task_id": "fixed-review", "verdict": "pass"},
        ]
    )
    terminal = records[0]
    monkeypatch.setattr(module, "_all_dispatch_records", lambda repo: records)
    monkeypatch.setattr(
        module,
        "accepted_review_terminals",
        lambda rows: {"fixed-review": terminal},
    )
    monkeypatch.setattr(
        module,
        "load_finding_records",
        lambda repo, **kwargs: [
            {
                "finding_id": "f_fixed",
                "review_task_id": "fixed-review",
                "state": "addressed",
                "snapshot_tree_sha": source_tree,
                "deterministic_evidence": {"target_tree_sha": target_tree},
            }
        ],
    )

    assert module.load_delta_edges(tmp_path, lens="code", source_ref=branch) == {
        source_tree: target_tree
    }


def test_chain_finding_evidence_ignores_security_named_category(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".audit" / "findings").mkdir(parents=True)
    branch = "refs/heads/feature/reviewed"
    source_tree = "1" * 40
    target_tree = "2" * 40
    task_id = "sectioned-review"
    terminal = current_dispatch_record(
        {
            "type": "attempt-terminal",
            "task_id": task_id,
            "status": "completed",
            "read_only": True,
            "work_kind": "review",
            "review_intent": "delivery-code-review",
            "category": "security-review",
            "task_contract": {
                "review_intent": "delivery-code-review",
                "category": "security-review",
            },
            "source_identity": {"ref": branch},
            "snapshot_tree_sha": source_tree,
            "review_chain_receipt": {
                "schema_version": "ReviewChainReceiptV1",
                "required_sections": ["code", "security"],
                "sections": {
                    "code": {"finding_ids": ["f_code"]},
                    "security": {"finding_ids": []},
                },
            },
        }
    )
    records = [
        terminal,
        current_dispatch_record(
            {"type": "verdict", "task_id": task_id, "verdict": "pass"}
        ),
    ]
    monkeypatch.setattr(module, "_all_dispatch_records", lambda repo: records)
    monkeypatch.setattr(
        module, "accepted_review_terminals", lambda rows: {task_id: terminal}
    )
    monkeypatch.setattr(
        module,
        "load_finding_records",
        lambda repo, **kwargs: [
            {
                "finding_id": "f_code",
                "review_task_id": task_id,
                "state": "addressed",
                "snapshot_tree_sha": source_tree,
                "deterministic_evidence": {"target_tree_sha": target_tree},
            }
        ],
    )
    monkeypatch.setattr(
        module,
        "_security_trigger_paths_between",
        lambda *args, **kwargs: ("scripts/util/review_chain.py",),
    )

    assert module.load_delta_edges(tmp_path, lens="security", source_ref=branch) == {}
    assert module.load_delta_edges(tmp_path, lens="code", source_ref=branch) == {
        source_tree: target_tree
    }


def _stub_evidence_transition(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    *,
    origins: tuple[str, ...],
    source_tree: str,
    target_tree: str,
    branch: str,
) -> None:
    (repo / ".audit" / "findings").mkdir(parents=True)
    terminals: dict[str, dict[str, object]] = {}
    records: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    for lens in origins:
        task_id = f"{lens}-review"
        contract = {"review_intent": "delivery-code-review", "review_lens": lens}
        terminal = current_dispatch_record(
            {
                "type": "attempt-terminal",
                "task_id": task_id,
                "status": "completed",
                "read_only": True,
                "work_kind": "review",
                **contract,
                "task_contract": contract,
                "source_identity": {"ref": branch},
                "snapshot_tree_sha": source_tree,
            }
        )
        terminals[task_id] = terminal
        records.extend(
            (
                terminal,
                current_dispatch_record(
                    {"type": "verdict", "task_id": task_id, "verdict": "pass"}
                ),
            )
        )
        findings.append(
            {
                "finding_id": f"f_{lens}",
                "review_task_id": task_id,
                "state": "addressed",
                "snapshot_tree_sha": source_tree,
                "deterministic_evidence": {"target_tree_sha": target_tree},
            }
        )
    monkeypatch.setattr(module, "_all_dispatch_records", lambda repo: records)
    monkeypatch.setattr(
        module,
        "accepted_review_terminals",
        lambda rows: terminals,
    )
    monkeypatch.setattr(
        module,
        "load_finding_records",
        lambda repo, **kwargs: findings,
    )


def test_security_finding_evidence_extends_the_standing_code_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    branch = "refs/heads/feature/reviewed"
    source_tree = "1" * 40
    target_tree = "2" * 40
    _stub_evidence_transition(
        monkeypatch,
        tmp_path,
        origins=("security",),
        source_tree=source_tree,
        target_tree=target_tree,
        branch=branch,
    )

    assert module.load_delta_edges(tmp_path, lens="code", source_ref=branch) == {
        source_tree: target_tree
    }


@pytest.mark.parametrize(
    ("path", "before", "after", "expected"),
    (
        ("service.py", "value = 1\n", "value = 2\n", True),
        ("fastapi_backend/app/config.py", "DEBUG = False\n", "SECRET = None\n", False),
    ),
)
def test_code_evidence_advances_security_only_without_a_new_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
    before: str,
    after: str,
    expected: bool,
) -> None:
    branch = "refs/heads/feature/reviewed"
    base_commit = _security_repo(tmp_path, {path: before})
    base = module.subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", f"{base_commit}^{{tree}}"], text=True
    ).strip()
    target_path = tmp_path / path
    target_path.write_text(after, encoding="utf-8")
    module.subprocess.run(["git", "-C", str(tmp_path), "add", path], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "fix"],
        check=True,
        capture_output=True,
    )
    target = module.subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    _stub_evidence_transition(
        monkeypatch,
        tmp_path,
        origins=("code",),
        source_tree=base,
        target_tree=target,
        branch=branch,
    )

    edges = module.load_delta_edges(tmp_path, lens="security", source_ref=branch)
    assert (edges == {base: target}) is expected


def test_mixed_origin_security_triggering_evidence_requires_security_delta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    branch = "refs/heads/feature/reviewed"
    _stub_evidence_transition(
        monkeypatch,
        tmp_path,
        origins=("code", "security"),
        source_tree="1" * 40,
        target_tree="2" * 40,
        branch=branch,
    )
    monkeypatch.setattr(
        module,
        "_security_trigger_paths_between",
        lambda *args, **kwargs: ("fastapi_backend/app/config.py",),
    )

    assert module.load_delta_edges(tmp_path, lens="security", source_ref=branch) == {}


def test_cross_lens_evidence_rejects_untrusted_tree_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    branch = "refs/heads/feature/reviewed"
    _stub_evidence_transition(
        monkeypatch,
        tmp_path,
        origins=("code",),
        source_tree="--output=/tmp/untrusted",
        target_tree="b" * 40,
        branch=branch,
    )
    monkeypatch.setattr(
        module,
        "_security_trigger_paths_between",
        lambda *args, **kwargs: pytest.fail("invalid trees must not reach git"),
    )

    assert module.load_delta_edges(tmp_path, lens="security", source_ref=branch) == {}


def test_cross_lens_security_scan_reuses_the_pinned_publication_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    branch = "refs/heads/feature/reviewed"
    source_tree = "a" * 40
    target_tree = "b" * 40
    pinned_runner = object()
    _stub_evidence_transition(
        monkeypatch,
        tmp_path,
        origins=("code",),
        source_tree=source_tree,
        target_tree=target_tree,
        branch=branch,
    )
    observed: list[object] = []

    def security_paths(*args, **kwargs):
        observed.append(kwargs.get("runner"))
        return ()

    monkeypatch.setattr(module, "_security_trigger_paths_between", security_paths)

    assert module.load_delta_edges(
        tmp_path,
        lens="security",
        source_ref=branch,
        runner=pinned_runner,
    ) == {source_tree: target_tree}
    assert observed == [pinned_runner]


def test_publish_accepts_delta_chain_coverage(tmp_path: Path) -> None:
    """L4 (#3106 A2): a delta chain ending at the head tree publishes."""
    chained = replace(
        request(),
        current_source_identity={
            "version": 2,
            "ref": "refs/heads/feature/2645-pr-publication",
            "head": "a" * 40,
            "state_sha256": "drifted-after-review",
        },
        review_snapshot_tree_sha="tree0",
        current_tree_sha="tree2",
        chain_covers_head=True,
    )

    assert module.publish(FakeRunner(), chained, evidence_dir=tmp_path).number == 2646

    broken = replace(chained, chain_covers_head=False)
    with pytest.raises(module.PublicationError, match="local source"):
        module.publish(FakeRunner(), broken, evidence_dir=tmp_path)


def test_publish_blocks_on_open_important_findings(tmp_path: Path) -> None:
    blocked = replace(request(), open_finding_ids=("f_dead", "f_beef"))

    runner = FakeRunner()
    with pytest.raises(module.PublicationError, match="open important\\+ findings"):
        module.publish(runner, blocked, evidence_dir=tmp_path)
    assert runner.calls == []


def test_open_important_finding_ids_filters_ref_severity_and_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    dispatch_dir = tmp_path / ".audit" / "dispatch"
    dispatch_dir.mkdir(parents=True)
    (dispatch_dir / "2026-08-27.jsonl").write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in [
                {
                    "type": "attempt-recovery",
                    "task_id": "rev-on-branch",
                    "source_identity": {"ref": "refs/heads/feature/x"},
                    "review_chain_receipt": {
                        "chain_id": "rc_primary",
                        "snapshot_tree_sha": "a" * 40,
                    },
                },
                {
                    "type": "review-chain-advisory",
                    "status": "completed",
                    "advisory": True,
                    "task_id": "cross-harness-primary",
                    "primary_task_id": "rev-on-branch",
                    "review_chain_advisory_receipt": {
                        "schema_version": "ReviewChainAdvisoryReceiptV1",
                        "chain_id": "rc_primary",
                        "primary_task_id": "rev-on-branch",
                        "advisory_task_id": "cross-harness-primary",
                        "snapshot_tree_sha": "a" * 40,
                    },
                },
                {
                    "type": "attempt-terminal",
                    "task_id": "rev-elsewhere",
                    "source_identity": {"ref": "refs/heads/other"},
                },
                {
                    "type": "attempt-terminal",
                    "task_id": "rev-superseded",
                    "source_identity": {"ref": "refs/heads/feature/x"},
                },
                {
                    "type": "attempt-supersession",
                    "task_id": "rev-superseded",
                    "superseded_task_id": "rev-superseded",
                },
            ]
        )
    )
    findings_dir = tmp_path / ".audit" / "findings"
    findings_dir.mkdir(parents=True)
    (findings_dir / "l.jsonl").write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in [
                {
                    "finding_id": "f_block",
                    "state": "open",
                    "severity": "important",
                    "review_task_id": "rev-on-branch",
                },
                {
                    "finding_id": "f_sugg",
                    "state": "open",
                    "severity": "suggestion",
                    "review_task_id": "rev-on-branch",
                },
                {
                    "finding_id": "f_demoted",
                    "state": "open",
                    "severity": "suggestion",
                    "original_severity": "important",
                    "review_task_id": "rev-on-branch",
                },
                {
                    "finding_id": "f_cross_harness",
                    "state": "open",
                    "severity": "important",
                    "advisory": True,
                    "review_task_id": "cross-harness-primary",
                },
                {
                    "finding_id": "f_local_advisory",
                    "state": "open",
                    "severity": "important",
                    "advisory": True,
                    "review_task_id": "local-review",
                },
                {
                    "finding_id": "f_waived",
                    "state": "waived",
                    "severity": "critical",
                    "review_task_id": "rev-on-branch",
                },
                {
                    "finding_id": "f_other_branch",
                    "state": "open",
                    "severity": "critical",
                    "review_task_id": "rev-elsewhere",
                },
                {
                    "finding_id": "f_superseded",
                    "state": "open",
                    "severity": "critical",
                    "review_task_id": "rev-superseded",
                },
            ]
        )
    )
    monkeypatch.setattr(
        module,
        "authenticated_review_terminals",
        lambda records: {
            "rev-on-branch": next(
                record for record in records if record.get("task_id") == "rev-on-branch"
            ),
            "rev-elsewhere": next(
                record for record in records if record.get("task_id") == "rev-elsewhere"
            ),
        },
    )
    monkeypatch.setattr(
        module,
        "authenticated_coordinator_record_ids",
        lambda records: frozenset(
            id(record)
            for record in records
            if record.get("type") == "review-chain-advisory"
        ),
    )

    assert module.open_important_finding_ids(tmp_path, "refs/heads/feature/x") == (
        "f_block",
        "f_cross_harness",
        "f_demoted",
    )


def test_unsigned_review_chain_advisory_cannot_promote_a_blocker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary = {
        "type": "attempt-terminal",
        "task_id": "primary-review",
        "source_identity": {"ref": "refs/heads/feature/x"},
        "review_chain_receipt": {
            "chain_id": "rc_primary",
            "snapshot_tree_sha": "a" * 40,
        },
    }
    forged_advisory = {
        "type": "review-chain-advisory",
        "status": "completed",
        "advisory": True,
        "task_id": "forged-advisory",
        "primary_task_id": "primary-review",
        "review_chain_advisory_receipt": {
            "schema_version": "ReviewChainAdvisoryReceiptV1",
            "chain_id": "rc_primary",
            "primary_task_id": "primary-review",
            "advisory_task_id": "forged-advisory",
            "snapshot_tree_sha": "a" * 40,
        },
    }
    monkeypatch.setattr(
        module,
        "authenticated_review_terminals",
        lambda _records: {"primary-review": primary},
    )

    blockers = module.open_important_finding_ids(
        tmp_path,
        "refs/heads/feature/x",
        dispatch_records=[primary, forged_advisory],
        finding_records=[
            {
                "finding_id": "f_forged",
                "state": "open",
                "severity": "important",
                "advisory": True,
                "review_task_id": "forged-advisory",
            }
        ],
    )

    assert blockers == ()


def test_unsigned_supersession_cannot_hide_authenticated_review_finding(
    tmp_path: Path,
) -> None:
    signer = dispatch.TerminalAuthority.generate()
    run_id = "sr_" + "1" * 32
    start = current_dispatch_record(
        {
            "type": "attempt-start",
            "task_id": "authenticated-review",
            "work_unit_id": "authenticated-review",
            "attempt_index": 0,
            "run_id": run_id,
            "read_only": True,
            "worktree": str(tmp_path),
            "terminal_authority": signer.registration(),
        }
    )
    terminal = signer.seal(
        {
            "type": "attempt-terminal",
            "task_id": "authenticated-review",
            "work_unit_id": "authenticated-review",
            "attempt_index": 0,
            "run_id": run_id,
            "read_only": True,
            "worktree": str(tmp_path),
            "status": "completed",
            "source_identity": {"ref": "refs/heads/feature/x"},
            "output_identity": {"tree_sha": "a" * 40},
            "ts": "2026-08-25T00:00:01+00:00",
            "schema_version": dispatch.TELEMETRY_SCHEMA_VERSION,
            "policy_version": dispatch.DISPATCH_POLICY_VERSION,
        },
        authority_kind="dispatcher",
    )
    forged = {
        **terminal,
        "type": "attempt-supersession",
        "status": "superseded",
        "supersession_reason": "stale-source",
        "superseded_task_id": "authenticated-review",
    }
    forged.pop("terminal_authority_proof")

    blockers = module.open_important_finding_ids(
        tmp_path,
        "refs/heads/feature/x",
        dispatch_records=[start, terminal, forged],
        finding_records=[
            {
                "finding_id": "f_must_block",
                "state": "open",
                "severity": "critical",
                "review_task_id": "authenticated-review",
            }
        ],
    )

    assert blockers == ("f_must_block",)


@pytest.mark.usefixtures("isolated_coordinator_authority")
def test_failed_review_supersession_keeps_findings_blocking_until_batch_retirement(
    tmp_path: Path,
) -> None:
    signer = dispatch.TerminalAuthority.generate()
    run_id = "sr_" + "3" * 32
    start = current_dispatch_record(
        {
            "type": "attempt-start",
            "task_id": "failed-review",
            "work_unit_id": "failed-review",
            "attempt_index": 0,
            "run_id": run_id,
            "read_only": True,
            "worktree": str(tmp_path),
            "terminal_authority": signer.registration(),
        }
    )
    terminal = signer.seal(
        current_dispatch_record(
            {
                "type": "attempt-terminal",
                "task_id": "failed-review",
                "work_unit_id": "failed-review",
                "attempt_index": 0,
                "run_id": run_id,
                "read_only": True,
                "work_kind": "review",
                "worktree": str(tmp_path),
                "status": "completed",
                "source_identity": {"ref": "refs/heads/feature/x"},
                "output_identity": {"tree_sha": "a" * 40},
                "review_chain_receipt": {
                    "chain_id": "rc_failed",
                    "snapshot_tree_sha": "a" * 40,
                },
                "ts": "2026-08-25T00:00:01+00:00",
            }
        ),
        authority_kind="dispatcher",
    )
    supersession = {
        **terminal,
        "type": "attempt-supersession",
        "status": "superseded",
        "failure_class": "failed-review",
        "supersession_reason": "failed-review",
        "superseded_task_id": "failed-review",
        "superseding_source_identity": terminal["source_identity"],
        "ts": "2026-08-25T00:00:02+00:00",
    }
    supersession.pop("terminal_authority_proof")
    # cmd_supersede preserves exact terminal identity fields, not the chain
    # projection. Publication must recover the authenticated primary receipt
    # from the original terminal while the supersession is standing.
    supersession.pop("review_chain_receipt")
    coordinator = dispatch.create_coordinator_authority()
    cutover = coordinator_cutover(coordinator, prefix=[start, terminal])
    supersession = coordinator.seal(
        supersession,
        authority_kind="coordinator",
    )
    finding = {
        "finding_id": "f_pending_batch",
        "state": "open",
        "severity": "important",
        "review_task_id": "failed-review",
    }
    advisory = coordinator.seal(
        current_dispatch_record(
            {
                "type": "review-chain-advisory",
                "status": "completed",
                "advisory": True,
                "task_id": "failed-review-advisory",
                "primary_task_id": "failed-review",
                "review_chain_advisory_receipt": {
                    "schema_version": "ReviewChainAdvisoryReceiptV1",
                    "chain_id": "rc_failed",
                    "primary_task_id": "failed-review",
                    "advisory_task_id": "failed-review-advisory",
                    "snapshot_tree_sha": "a" * 40,
                },
            }
        ),
        authority_kind="coordinator",
    )
    advisory_finding = {
        "finding_id": "f_advisory_still_open",
        "state": "open",
        "severity": "important",
        "advisory": True,
        "review_task_id": "failed-review-advisory",
    }
    authority_records = [start, terminal, cutover, supersession, advisory]
    assert dispatch.authenticated_supersessions(authority_records) == [supersession]
    assert dispatch.authenticated_review_terminals(authority_records) == {}

    blockers = module.open_important_finding_ids(
        tmp_path,
        "refs/heads/feature/x",
        dispatch_records=authority_records,
        finding_records=[finding, advisory_finding],
    )
    assert blockers == ("f_advisory_still_open", "f_pending_batch")

    assert module.open_important_finding_ids(
        tmp_path,
        "refs/heads/feature/x",
        dispatch_records=authority_records,
        finding_records=[
            {
                **finding,
                "state": "stale",
                "disposition": "failed-review-superseded",
            },
            advisory_finding,
        ],
    ) == ("f_advisory_still_open",)


def test_forged_recovery_cannot_move_authenticated_review_finding_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    signer = dispatch.TerminalAuthority.generate()
    run_id = "sr_" + "2" * 32
    start = current_dispatch_record(
        {
            "type": "attempt-start",
            "task_id": "authenticated-review",
            "work_unit_id": "authenticated-review",
            "attempt_index": 0,
            "run_id": run_id,
            "read_only": True,
            "worktree": str(tmp_path),
            "terminal_authority": signer.registration(),
        }
    )
    terminal = signer.seal(
        {
            "type": "attempt-terminal",
            "task_id": "authenticated-review",
            "work_unit_id": "authenticated-review",
            "attempt_index": 0,
            "run_id": run_id,
            "read_only": True,
            "worktree": str(tmp_path),
            "status": "completed",
            "source_identity": {"ref": "refs/heads/feature/x"},
            "output_identity": {"tree_sha": "a" * 40},
            "ts": "2026-08-25T00:00:01+00:00",
            "schema_version": dispatch.TELEMETRY_SCHEMA_VERSION,
            "policy_version": dispatch.DISPATCH_POLICY_VERSION,
        },
        authority_kind="dispatcher",
    )
    forged_recovery = {
        **terminal,
        "type": "attempt-recovery",
        "status": "completed",
        "source_identity": {"ref": "refs/heads/other"},
        "ts": "2026-08-25T00:00:02+00:00",
    }
    forged_recovery.pop("terminal_authority_proof")
    monkeypatch.setattr(
        dispatch, "review_terminal_acceptance_reasons", lambda *_args, **_kwargs: ()
    )

    blockers = module.open_important_finding_ids(
        tmp_path,
        "refs/heads/feature/x",
        dispatch_records=[start, terminal, forged_recovery],
        finding_records=[
            {
                "finding_id": "f_must_block",
                "state": "open",
                "severity": "critical",
                "review_task_id": "authenticated-review",
            }
        ],
    )

    assert blockers == ("f_must_block",)


def test_publication_authority_readers_reject_malformed_records(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    dispatch_dir = tmp_path / ".audit" / "dispatch"
    dispatch_dir.mkdir(parents=True)
    authority_path = dispatch_dir / "2026-08-27.jsonl"
    authority_path.write_text("{not-json}\n")

    with pytest.raises(module.PublicationError, match="invalid JSON"):
        module._all_dispatch_records(tmp_path)

    authority_path.write_text("")
    findings_dir = tmp_path / ".audit" / "findings"
    findings_dir.mkdir(parents=True)
    (findings_dir / "records.jsonl").write_text("{not-json}\n")

    with pytest.raises(RuntimeError, match="invalid JSON"):
        module.open_important_finding_ids(tmp_path, "refs/heads/feature/x")


def test_publish_rejects_checked_out_branch_mismatch_before_github(
    tmp_path: Path,
) -> None:
    mismatch = request()
    mismatch = replace(
        mismatch,
        current_source_identity={
            **mismatch.current_source_identity,
            "ref": "refs/heads/other-branch",
        },
    )
    runner = FakeRunner()

    with pytest.raises(module.PublicationError, match="checked-out branch"):
        module.publish(runner, mismatch, evidence_dir=tmp_path)

    assert runner.calls == []


def test_post_create_head_mismatch_closes_pr_and_fails(tmp_path: Path) -> None:
    runner = FakeRunner(fresh_head="b" * 40)

    with pytest.raises(module.PublicationError, match="quarantined as closed"):
        module.publish(runner, request(), evidence_dir=tmp_path)

    assert any(
        args[-1] == "repos/{owner}/{repo}/pulls/2646"
        and "PATCH" in args
        and payload == {"state": "closed"}
        for args, payload in runner.calls
    )
    record = json.loads(next(tmp_path.glob("*.jsonl")).read_text().strip())
    assert record["status"] == "head_mismatch_quarantined"


def test_publish_adopts_only_matching_existing_pr(tmp_path: Path) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    runner = FakeRunner(existing=[matching])

    result = module.publish(runner, request(), evidence_dir=tmp_path)

    assert result.number == 2646
    assert not any(
        args[-1] == "repos/{owner}/{repo}/pulls" and "POST" in args
        for args, _payload in runner.calls
    )


def test_published_evidence_retry_reuses_identical_authority(tmp_path: Path) -> None:
    record = {
        **vars(request()),
        "adopted": False,
        "pr": 2646,
        "run_id": "sr_" + "a" * 32,
        "status": "published",
        "url": "https://example.test/pull/2646",
    }

    first = module._write_published_evidence_once(tmp_path, record, generation=0)
    second = module._write_published_evidence_once(
        tmp_path, {**record, "adopted": True}, generation=0
    )

    assert second == first
    assert len(first.read_text(encoding="utf-8").splitlines()) == 1


def test_preflight_existing_adoption_rejects_invalid_live_body_without_mutation(
    tmp_path: Path,
) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "9" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    runner = FakeRunner(existing=[matching], fresh_body="Summary only")

    with pytest.raises(module.PublicationError, match="publication policy"):
        module.preflight_existing_adoption(runner, request())

    assert runner.calls
    assert all("GET" in args for args, _payload in runner.calls)


def test_preflight_existing_adoption_rejects_live_body_drift_without_mutation() -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "9" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    requested_body = context_body(
        "Closes #2645\n\nObligation-change: The weaker rule is intentional."
    )
    runner = FakeRunner(existing=[matching], fresh_body=VALID_BODY)

    with pytest.raises(module.PublicationError, match="differs from requested body"):
        module.preflight_existing_adoption(runner, request(body=requested_body))

    assert runner.calls
    assert all("GET" in args for args, _payload in runner.calls)


def test_publish_rejects_adopted_pr_with_wrong_live_skill_run_marker(
    tmp_path: Path,
) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    marker = "<!-- skill-run-id: sr_" + "a" * 32 + " -->"
    requested_body = context_body(f"Closes #2645\n{marker}")
    runner = FakeRunner(existing=[matching], fresh_body=VALID_BODY)

    with pytest.raises(module.PublicationError, match="skill-run marker"):
        module.publish(
            runner,
            request(body=requested_body),
            evidence_dir=tmp_path,
        )


def test_publish_rejects_adopted_pr_with_invalid_live_body_policy(
    tmp_path: Path,
) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    marker = "<!-- skill-run-id: sr_" + "a" * 32 + " -->"
    runner = FakeRunner(existing=[matching], fresh_body=f"Summary only\n{marker}")

    with pytest.raises(module.PublicationError, match="publication policy"):
        module.publish(
            runner,
            request(body=context_body(f"Closes #2645\n{marker}")),
            evidence_dir=tmp_path,
        )


def test_publish_rejects_adopted_pr_without_plain_language_opening(
    tmp_path: Path,
) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    runner = FakeRunner(existing=[matching], fresh_body="Closes #2645")

    with pytest.raises(module.PublicationError, match="Context and goal"):
        module.publish(runner, request(), evidence_dir=tmp_path)


def test_publish_rejects_adopted_pr_without_required_validation(
    tmp_path: Path,
) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    live_body = context_body().split("\n\n## Validation", 1)[0]
    runner = FakeRunner(existing=[matching], fresh_body=live_body)

    with pytest.raises(module.PublicationError, match="Validation"):
        module.publish(runner, request(), evidence_dir=tmp_path)


def test_publish_rejects_adopted_pr_without_string_live_body(tmp_path: Path) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    runner = FakeRunner(existing=[matching], fresh_body=None)

    with pytest.raises(module.PublicationError, match="has no live body"):
        module.publish(runner, request(), evidence_dir=tmp_path)


@pytest.mark.parametrize("live_body", ["Closes #2645", "", "Standalone-Reason:   "])
def test_publish_rejects_standalone_label_when_adopted_body_lacks_reason(
    tmp_path: Path,
    live_body: str,
) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    runner = FakeRunner(existing=[matching], fresh_body=live_body)

    with pytest.raises(module.PublicationError, match="live body lacks"):
        module.publish(
            runner,
            request(
                body=context_body("Standalone-Reason: recursive component audit"),
                labels=("standalone",),
            ),
            evidence_dir=tmp_path,
        )

    assert not any("/labels" in args[-1] for args, _payload in runner.calls)


def test_publish_quarantines_new_standalone_pr_when_fresh_body_lacks_reason(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(fresh_body=VALID_BODY)

    with pytest.raises(module.PublicationError, match="live body lacks"):
        module.publish(
            runner,
            request(
                body=context_body("Standalone-Reason: recursive component audit"),
                labels=("standalone",),
            ),
            evidence_dir=tmp_path,
        )

    assert any(
        args[-1] == "repos/{owner}/{repo}/pulls/2646"
        and "PATCH" in args
        and payload == {"state": "closed"}
        for args, payload in runner.calls
    )


def test_publish_rejects_adopted_pr_live_body_drift_after_visual_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    proposed_body = context_body(
        "Closes #2645\n\n![repair](https://example.test/repair.png)"
    )
    runner = FakeRunner(existing=[matching], fresh_body=VALID_BODY)
    publication = replace(
        request(body=proposed_body, labels=("ci",)),
        changed_paths=("nextjs-frontend/components/repair-card.tsx",),
    )

    with pytest.raises(module.PublicationError, match="differs from requested body"):
        module.publish(runner, publication, evidence_dir=tmp_path)

    assert "visual-evidence advisory" in capsys.readouterr().err


def test_publish_adopts_standalone_pr_when_live_body_has_reason(
    tmp_path: Path,
) -> None:
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    body = context_body("Standalone-Reason: recursive component audit")
    runner = FakeRunner(existing=[matching], fresh_body=body)

    result = module.publish(
        runner,
        request(body=body, labels=("standalone",)),
        evidence_dir=tmp_path,
    )

    assert result.number == 2646
    assert not any(
        args[-1] == "repos/{owner}/{repo}/pulls" and "POST" in args
        for args, _payload in runner.calls
    )
    assert any("/labels" in args[-1] for args, _payload in runner.calls)


def test_load_review_receipt_requires_pass_and_rejects_supersession(
    tmp_path: Path,
) -> None:
    dispatch_dir = tmp_path / ".audit" / "dispatch"
    dispatch_dir.mkdir(parents=True)
    identity = {
        "version": 2,
        "ref": "refs/heads/feature/reviewed",
        "head": "a" * 40,
        "state_sha256": "state",
    }
    path = dispatch_dir / "2026-07-13.jsonl"
    records = current_dispatch_records(
        [
            {
                "type": "attempt-terminal",
                "task_id": "final-review",
                "status": "completed",
                "work_kind": "review",
                "read_only": True,
                "review_intent": "delivery-code-review",
                "review_lens": "code",
                "task_contract": {
                    "review_intent": "delivery-code-review",
                    "review_lens": "code",
                },
                "task_contract_hash": dispatch.task_contract_hash(
                    {
                        "review_intent": "delivery-code-review",
                        "review_lens": "code",
                    }
                ),
                "source_identity": identity,
            },
            {
                "type": "verdict",
                "task_id": "final-review",
                "verdict": "pass",
            },
        ]
    )
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    assert module.load_review_source_identity(tmp_path, "final-review") == (
        identity,
        None,
        "code",
    )

    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                current_dispatch_record(
                    {"type": "verdict", "task_id": "final-review", "verdict": "fail"}
                )
            )
            + "\n"
        )
    with pytest.raises(module.PublicationError, match="latest independent verdict"):
        module.load_review_source_identity(tmp_path, "final-review")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                current_dispatch_record(
                    {"type": "verdict", "task_id": "final-review", "verdict": "pass"}
                )
            )
            + "\n"
        )

    # An advisory review is not publication evidence even when completed,
    # verdicted, and identity-clean (#3106 A1).
    advisory_path = dispatch_dir / "2026-07-14.jsonl"
    advisory_path.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in current_dispatch_records(
                [
                    {
                        "type": "attempt-terminal",
                        "task_id": "advisory-review",
                        "status": "completed",
                        "work_kind": "review",
                        "read_only": True,
                        "advisory": True,
                        "source_identity": identity,
                    },
                    {
                        "type": "verdict",
                        "task_id": "advisory-review",
                        "verdict": "pass",
                    },
                ]
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.PublicationError, match="no accepted read-only"):
        module.load_review_source_identity(tmp_path, "advisory-review")

    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                current_dispatch_record(
                    {"type": "attempt-supersession", "task_id": "final-review"}
                )
            )
            + "\n"
        )
    with pytest.raises(module.PublicationError, match="superseded"):
        module.load_review_source_identity(tmp_path, "final-review")


@pytest.mark.usefixtures("isolated_coordinator_authority")
def test_load_review_rejects_unsigned_verdict_for_registered_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = {
        "version": 2,
        "ref": "refs/heads/feature/reviewed",
        "head": "a" * 40,
        "state_sha256": "state",
    }
    terminal = current_dispatch_record(
        {
            "type": "attempt-terminal",
            "task_id": "registered-review",
            "run_id": "sr_" + "1" * 32,
            "status": "completed",
            "worker_identity": "codex:gpt-5.6-sol",
            "effective_alias": "sol",
            "review_intent": "delivery-code-review",
            "review_lens": "code",
            "task_contract": {
                "review_intent": "delivery-code-review",
                "review_lens": "code",
            },
            "source_identity": identity,
            "terminal_authority_proof": {"authority_kind": "dispatcher"},
        }
    )
    unsigned = current_dispatch_record(
        {
            "type": "verdict",
            "task_id": "registered-review",
            "run_id": terminal["run_id"],
            "verdict": "pass",
            "verifier_identity": "reviewer:root",
            "verifier_alias": "human",
            "target_worker_identity": terminal["worker_identity"],
        }
    )
    records = [terminal, unsigned]
    monkeypatch.setattr(module, "_all_dispatch_records", lambda _repo: records)
    monkeypatch.setattr(module, "authenticated_supersessions", lambda _records: [])
    monkeypatch.setattr(
        module,
        "accepted_review_terminals",
        lambda _records: {"registered-review": terminal},
    )

    with pytest.raises(module.PublicationError, match="latest independent verdict"):
        module.load_review_source_identity(tmp_path, "registered-review")

    coordinator = dispatch.create_coordinator_authority()
    cutover = coordinator_cutover(coordinator, prefix=records)
    signed = coordinator.seal(
        {
            "ts": "2026-08-25T00:00:02+00:00",
            **unsigned,
            "schema_version": dispatch.TELEMETRY_SCHEMA_VERSION,
            "policy_version": dispatch.DISPATCH_POLICY_VERSION,
        },
        authority_kind="coordinator",
    )
    records.extend([cutover, signed])
    assert module.load_review_source_identity(tmp_path, "registered-review") == (
        identity,
        None,
        "code",
    )
    monkeypatch.setattr(
        module,
        "_all_dispatch_records",
        lambda _repo: pytest.fail("frozen publication authority was reloaded"),
    )
    assert module.load_review_source_identity(
        tmp_path,
        "registered-review",
        dispatch_records=records,
    ) == (identity, None, "code")


def test_one_chain_receipt_satisfies_code_and_triggered_security_sections(
    tmp_path: Path,
) -> None:
    dispatch_dir = tmp_path / ".audit" / "dispatch"
    dispatch_dir.mkdir(parents=True)
    identity = {
        "version": 2,
        "ref": "refs/heads/feature/reviewed",
        "head": "a" * 40,
        "state_sha256": "state",
    }
    task = {
        "task_id": "chain-review",
        "work_kind": "review",
        "review_intent": "delivery-code-review",
        "required_sections": ["code", "security"],
        "security_trigger_paths": ["app/config.py"],
        "acceptance_commands": [],
    }
    result = {
        "review_sections": {
            "code": {
                "completion": "completed",
                "verdict": "clean",
                "findings": [],
            },
            "security": {
                "completion": "completed",
                "verdict": "clean",
                "threat_model_summary": "Config crosses a process trust boundary.",
                "findings": [],
            },
        },
        "findings": [],
    }
    patch_identity = {"schema_version": "PatchIdentityV1"}
    receipt = dispatch.build_review_chain_receipt(
        task=task,
        snapshot_sha="b" * 40,
        snapshot_tree_sha="c" * 40,
        patch_identity=patch_identity,
        result=result,
        finding_ids=[],
    )
    terminal = current_dispatch_record(
        {
            "type": "attempt-terminal",
            "task_id": "chain-review",
            "status": "completed",
            "work_kind": "review",
            "read_only": True,
            "review_intent": "delivery-code-review",
            "task_contract": task,
            "task_contract_hash": dispatch.task_contract_hash(task),
            "source_identity": identity,
            "snapshot_sha": "b" * 40,
            "snapshot_tree_sha": "c" * 40,
            "patch_identity": patch_identity,
            "review_chain_receipt": receipt,
        }
    )
    (dispatch_dir / "2026-08-17.jsonl").write_text(
        json.dumps(terminal)
        + "\n"
        + json.dumps(
            current_dispatch_record(
                {"type": "verdict", "task_id": "chain-review", "verdict": "pass"}
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert module.load_review_source_identity(
        tmp_path, "chain-review", expected_lens="code"
    ) == (identity, "c" * 40, "code")
    assert module.load_review_source_identity(
        tmp_path, "chain-review", expected_lens="security"
    ) == (identity, "c" * 40, "security")


def test_chain_delta_edge_advances_each_completed_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    branch = "refs/heads/feature/reviewed"
    contract = {
        "review_intent": "delivery-code-review",
        "required_sections": ["code", "security"],
        "security_trigger_paths": ["app/config.py"],
    }
    terminal = {
        "type": "attempt-terminal",
        "task_id": "chain-delta",
        "status": "completed",
        "review_intent": "delivery-code-review",
        "category": "code-review",
        "task_contract": contract,
        "review_chain_receipt": {
            "required_sections": ["code", "security"],
            "sections": {"code": {}, "security": {}},
        },
        "source_identity": {"ref": branch},
        "delta_from_tree_sha": "a" * 40,
        "snapshot_tree_sha": "b" * 40,
    }
    records = current_dispatch_records(
        [
            terminal,
            {"type": "verdict", "task_id": "chain-delta", "verdict": "pass"},
        ]
    )
    terminal = records[0]
    monkeypatch.setattr(
        module,
        "accepted_review_terminals",
        lambda rows: {"chain-delta": terminal},
    )

    assert module.load_delta_edges(
        tmp_path,
        lens="security",
        source_ref=branch,
        dispatch_records=records,
        finding_records=[],
    ) == {"a" * 40: "b" * 40}


def test_load_review_receipt_requires_dispatcher_acceptance_when_declared(
    tmp_path: Path,
) -> None:
    dispatch_dir = tmp_path / ".audit" / "dispatch"
    dispatch_dir.mkdir(parents=True)
    identity = {
        "version": 2,
        "ref": "refs/heads/feature/reviewed",
        "head": "a" * 40,
        "state_sha256": "state",
    }
    contract = {
        "acceptance_commands": ["true"],
        "review_intent": "delivery-code-review",
        "review_lens": "code",
    }
    records = current_dispatch_records(
        [
            {
                "type": "attempt-terminal",
                "task_id": "receiptless-review",
                "status": "completed",
                "work_kind": "review",
                "read_only": True,
                "review_intent": "delivery-code-review",
                "review_lens": "code",
                "worktree": str(tmp_path),
                "source_identity": identity,
                "snapshot_sha": "b" * 40,
                "snapshot_tree_sha": "c" * 40,
                "task_contract": contract,
                "task_contract_hash": dispatch.task_contract_hash(contract),
                "acceptance": [{"command": "true", "exit_code": 0, "tail": "pass"}],
            },
            {
                "type": "verdict",
                "task_id": "receiptless-review",
                "verdict": "pass",
            },
        ]
    )
    (dispatch_dir / "2026-08-07.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    with pytest.raises(
        module.PublicationError, match="missing-review-acceptance-receipt"
    ):
        module.load_review_source_identity(tmp_path, "receiptless-review")


@pytest.mark.parametrize(
    ("intent", "lens"),
    (("resolution-adjudication", "code"), ("delivery-code-review", "security")),
)
def test_publication_rejects_wrong_review_authority(
    tmp_path: Path, intent: str, lens: str
) -> None:
    dispatch_dir = tmp_path / ".audit" / "dispatch"
    dispatch_dir.mkdir(parents=True)
    contract = {"review_intent": intent, "review_lens": lens}
    records = current_dispatch_records(
        [
            {
                "type": "attempt-terminal",
                "task_id": "wrong-authority",
                "status": "completed",
                "work_kind": "review",
                "read_only": True,
                "review_intent": intent,
                "review_lens": lens,
                "source_identity": {
                    "version": 2,
                    "ref": "refs/heads/feature/reviewed",
                },
                "task_contract": contract,
                "task_contract_hash": dispatch.task_contract_hash(contract),
            },
            {"type": "verdict", "task_id": "wrong-authority", "verdict": "pass"},
        ]
    )
    (dispatch_dir / "2026-08-27.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    with pytest.raises(module.PublicationError, match="hash-bound"):
        module.load_review_source_identity(tmp_path, "wrong-authority")


def _security_repo(tmp_path: Path, files: dict[str, str]) -> str:
    module.subprocess.run(
        ["git", "init", str(tmp_path)], check=True, capture_output=True
    )
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True
    )
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    module.subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    module.subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    return module.subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()


@pytest.mark.parametrize("outdated", [False, True])
@pytest.mark.parametrize("boundary", ["preflight", "publish"])
def test_publication_preflight_refuses_unresolved_threads(
    outdated, boundary, monkeypatch, tmp_path
):
    matching = {
        "number": 2646,
        "html_url": "https://example.test/pull/2646",
        "head": {
            "ref": "feature/2645-pr-publication",
            "sha": "a" * 40,
            "repo": {"full_name": "feder-positronics/intelflo"},
        },
        "base": {"ref": "main"},
        "state": "open",
    }
    runner = FakeRunner(existing=[matching])
    original = runner.run_json

    def run_json(args, **kwargs):
        if "graphql" in args:
            runner.calls.append((args, None))
            return {
                "data": {
                    "resource": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "thread-1",
                                    "isResolved": False,
                                    "isOutdated": outdated,
                                    "path": "scripts/util/pr_publish.py",
                                    "comments": {
                                        "nodes": [
                                            {"url": "https://example.test/thread/1"}
                                        ]
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        return original(args, **kwargs)

    monkeypatch.setattr(runner, "run_json", run_json)
    with pytest.raises(module.PublicationError, match="thread-1"):
        if boundary == "preflight":
            module.preflight_existing_adoption(runner, request())
        else:
            module.publish(runner, request(), evidence_dir=tmp_path)
    assert all("POST" not in args and "PATCH" not in args for args, _ in runner.calls)


def test_reentry_invalidates_publication_and_same_head_is_reminted(tmp_path):
    risk = importlib.import_module("loopzero.review.risk")
    record = {
        "status": "published",
        "pr": 2646,
        "run_id": "sr_" + "1" * 32,
        "expected_head": "a" * 40,
        "review_task_id": "first",
    }
    path = module._write_published_evidence_once(tmp_path, record, generation=0)
    identity = {key: record[key] for key in ("pr", "run_id", "expected_head")}
    assert (
        risk.load_publication_review_risk(tmp_path, **identity, generation=0)[
            "review_task_id"
        ]
        == "first"
    )
    with pytest.raises(risk.ReviewRiskError, match="missing"):
        risk.load_publication_review_risk(tmp_path, **identity, generation=1)
    module._write_published_evidence_once(
        tmp_path, {**record, "review_task_id": "second"}, generation=1
    )
    assert (
        risk.load_publication_review_risk(tmp_path, **identity, generation=1)[
            "review_task_id"
        ]
        == "second"
    )
    module._write_published_evidence_once(
        tmp_path, {**record, "review_task_id": "second"}, generation=1
    )
    assert len(path.read_text().splitlines()) == 2


def test_bind_review_reentry_refuses_uncited_and_stale_reviews(monkeypatch):
    """After a reentry, the T0 exemption and pre-reentry reviews cannot remint."""
    reentry = importlib.import_module("loopzero.delivery.reentry")
    authority = importlib.import_module("loopzero.review.authority")
    run_id = "sr_" + "1" * 32
    decision = {
        "type": "delivery-control",
        "action": "review-reentry",
        "run_id": run_id,
        "ts": "2026-09-07T12:10:00+00:00",
    }
    monkeypatch.setattr(
        authority, "delivery_controller_records", lambda records: list(records)
    )
    before = {"ts": "2026-09-07T12:20:00Z"}
    after = {"ts": "2026-09-07T12:00:00Z"}
    rows = [before, decision, after]
    monkeypatch.setattr(
        authority,
        "accepted_review_terminals",
        lambda records: {"before": before, "after": after},
    )
    assert publish_risk.bind_review_reentry([], run_id=run_id, review_task_ids=[]) == 0
    with pytest.raises(module.PublicationError, match="exemption is unavailable"):
        publish_risk.bind_review_reentry(rows, run_id=run_id, review_task_ids=[])
    with pytest.raises(module.PublicationError, match="accepted after the reentry"):
        publish_risk.bind_review_reentry(
            rows, run_id=run_id, review_task_ids=["after", "before"]
        )
    assert (
        publish_risk.bind_review_reentry(rows, run_id=run_id, review_task_ids=["after"])
        == 1
    )
    assert reentry.publication_generation([decision], run_id) == 1


def test_publication_generation_binds_to_frozen_authority_records(
    tmp_path, monkeypatch
):
    """The reentry trust root is the frozen ledger, never the evidence path."""
    reentry = importlib.import_module("loopzero.delivery.reentry")
    authority = importlib.import_module("loopzero.review.authority")
    run_id = "sr_" + "1" * 32
    rows = [{"type": "delivery-control", "action": "review-reentry", "run_id": run_id}]
    monkeypatch.setattr(
        authority, "delivery_controller_records", lambda records: list(records)
    )
    evidence_dir = tmp_path / "elsewhere" / "pr-publications"
    record = {
        "status": "published",
        "pr": 2646,
        "run_id": run_id,
        "expected_head": "a" * 40,
        "review_task_id": "first",
    }
    generation = reentry.publication_generation(rows, run_id)
    assert generation == 1
    module._write_published_evidence_once(evidence_dir, record, generation=generation)
    written = [
        json.loads(line)
        for path in evidence_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    assert [row["review_reentry_generation"] for row in written] == [1]
