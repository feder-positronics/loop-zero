"""Near-verbatim evidence and delta-authority tests moved from IntelFlo."""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.config import Alias
from loopzero.kernel import authority as kernel_authority
from loopzero.kernel import authority_projection, authority_store, gitscope, patch_identity, policy
from loopzero.review import acceptance, authority, evidence, findings, harness, routing
from loopzero.runners.contract import MAX_RESULT_BYTES, RESULT_FIELDS
from loopzero.trust import system_executable

sys.modules.setdefault("dispatch_authority_store", authority_store)

_TARGET_MODULES = (
    evidence, authority, findings, harness, routing, acceptance,
    kernel_authority, authority_projection, authority_store, gitscope, policy,
)


class _MovedFacade:
    __file__ = evidence.__file__
    patch_identity = patch_identity
    dispatch_review_authority = authority
    system_executable = staticmethod(system_executable)

    @staticmethod
    def worktree_head(worktree):
        return evidence._snapshot_git(worktree, "rev-parse", "HEAD")

    @staticmethod
    def append_finding_records(*args, **kwargs):
        kwargs.setdefault("pr", 1)
        return evidence.append_finding_records(*args, **kwargs)

    @staticmethod
    def record_local_findings(*args, **kwargs):
        kwargs.setdefault("pr", 1)
        return evidence.record_local_findings(*args, **kwargs)

    @staticmethod
    def persist_result_artifact(repo, *, task_id, result, artifact_suffix=None):
        selected = {key: result.get(key) for key in RESULT_FIELDS if key in result}
        for key in (
            "acceptance",
            "findings",
            "modified_existing_tests",
            "modified_existing_tests_diagnostic",
            "attempts",
            "lineage",
            "provider_auth_hold",
        ):
            if key in result:
                selected[key] = result[key]
        payload = json.dumps(
            selected, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        payload_bytes = payload.encode("utf-8")
        if len(payload_bytes) > MAX_RESULT_BYTES:
            raise gitscope.DispatchError(
                f"result artifact for {task_id!r} exceeds the safe byte limit"
            )
        directory = repo / Path(".audit/dispatch/results")
        directory.mkdir(parents=True, exist_ok=True)
        safe_task_id = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
        suffix = f"-{artifact_suffix}" if artifact_suffix else ""
        path = directory / f"{safe_task_id}{suffix}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
        return str(path.relative_to(repo)), hashlib.sha256(payload_bytes).hexdigest()

    def __getattr__(self, name):
        for target in _TARGET_MODULES:
            if hasattr(target, name):
                return getattr(target, name)
        raise AttributeError(name)


module = _MovedFacade()


def _setattr_dispatch(monkeypatch, name, value, *args, **kwargs):
    matched = False
    for target in _TARGET_MODULES:
        if hasattr(target, name):
            monkeypatch.setattr(target, name, value, *args, **kwargs)
            matched = True
    if not matched:
        monkeypatch.setattr(module, name, value, *args, **kwargs)


@pytest.fixture(autouse=True)
def _legacy_mechanism_configuration(monkeypatch, tmp_path, isolated_ptrace_scope_path):
    profile = SimpleNamespace(
        root=tmp_path, audit_root=Path(".audit"),
        review_snapshot_namespace="dispatch-snapshots",
        finding_snapshot_namespace="finding-snapshots",
        github=SimpleNamespace(ref_namespace="refs/heads"),
    )
    evidence.configure(profile)
    routing.configure(
        SimpleNamespace(
            aliases={
                "sol": Alias("codex", "gpt-5.6-sol", family="gpt"),
                "astra": Alias("codex", "gpt-6-astra", family="gpt"),
            },
            tiers={},
            routing_budgets={"medium": 5.0, "high": 10.0},
            routing_policy_version="2026-08-17-v11",
            telemetry_schema_version="dispatch-telemetry-v9",
            compatible_policy_versions=("2026-08-17-v11",),
            default_timeout_s=900,
            engine_cooldown_s=600,
            audit_root=Path(".audit"),
            env_prefix="INTELFLO",
            verifier_models={"sol": "gpt-5.6-sol", "astra": "gpt-6-astra"},
        )
    )
    monkeypatch.setattr(kernel_authority, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
    monkeypatch.setattr(authority_store, "_coordinator_state_directory", lambda: tmp_path / "dispatch-authority", raising=False)
    monkeypatch.setattr(kernel_authority, "_coordinator_state_directory", lambda: tmp_path / "dispatch-authority", raising=False)
    monkeypatch.setattr(findings, "_REQUIRE_PR_SCOPE", False)
    monkeypatch.setattr(findings, "_CONFIGURED_ROOT", tmp_path)
    monkeypatch.setattr(findings, "FINDINGS_DIR", Path(".audit/findings"))
    monkeypatch.setattr(findings, "OPERATIONS_DIR", Path(".audit/finding-operations"))


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    (tmp_path / "allowed.py").write_text("original\n")
    (tmp_path / ".gitignore").write_text("fastapi_backend/.venv/\n")
    (tmp_path / "fastapi_backend" / ".venv").mkdir(parents=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "allowed.py", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    (tmp_path / ".branch-marker").write_text("review branch\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".branch-marker"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "branch baseline"], check=True)
    return tmp_path


def governed(records):
    stamped = []
    for record in records:
        value = {**record, "schema_version": policy.TELEMETRY_SCHEMA_VERSION, "policy_version": policy.DISPATCH_POLICY_VERSION}
        if value.get("type") == "attempt-terminal":
            contract = gitscope.immutable_task_contract(value)
            value.setdefault("task_contract", contract)
            value.setdefault("task_contract_hash", gitscope.task_contract_hash(contract))
        stamped.append(value)
    return stamped


def governed_result(task_id, *, status="completed", summary="done"):
    return {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "files_changed": [],
        "commit": None,
        "tests": [],
        "requirements_met": [],
        "risks": [],
        "decisions_made": [],
        "escalation_reason": None,
        "recommended_followups": [],
    }


class TestReviewSnapshot:
    def test_authority_git_reads_use_the_trusted_executable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            stdout = "a" * 40 + "\n" if kwargs.get("text") else b"patch\n"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

        _setattr_dispatch(
            monkeypatch, "system_executable", lambda _name: Path("/usr/bin/git")
        )
        monkeypatch.setattr(module.subprocess, "run", run)
        monkeypatch.setenv("GIT_DIR", "/attacker/repo.git")
        monkeypatch.setenv("GIT_WORK_TREE", "/attacker/worktree")

        assert module.worktree_head(tmp_path) == "a" * 40
        snapshot = module.ReviewSnapshot("b" * 40, "c" * 40, tmp_path)
        assert (
            module.materialize_delta_review_patch(
                tmp_path, delta_from="a" * 40, new_snapshot=snapshot
            )
            == "patch\n"
        )
        assert calls
        assert all(command[0] == "/usr/bin/git" for command, _kwargs in calls)
        for _command, kwargs in calls:
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            assert "GIT_DIR" not in environment
            assert "GIT_WORK_TREE" not in environment

    def test_snapshot_git_ignores_hostile_path(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        hostile = tmp_path / "hostile-snapshot-path"
        hostile.mkdir()
        marker = tmp_path / "fake-snapshot-git-ran"
        fake_git = hostile / "git"
        fake_git.write_text(
            f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexit 91\n", encoding="utf-8"
        )
        fake_git.chmod(fake_git.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", str(hostile))
        resolved: list[str] = []
        original_resolver = module.system_executable

        def resolve(executable: str):
            resolved.append(executable)
            return original_resolver(executable)

        _setattr_dispatch(monkeypatch, "system_executable", resolve)

        assert module._snapshot_git(git_repo, "rev-parse", "HEAD")
        assert not marker.exists()
        assert resolved == ["git"]

    def test_snapshot_git_ignores_inherited_repository_overrides(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected_head = module._snapshot_git(git_repo, "rev-parse", "HEAD")
        attacker = git_repo.parent / "attacker-snapshot"
        subprocess.run(["git", "init", "-q", str(attacker)], check=True)
        subprocess.run(
            ["git", "-C", str(attacker), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(attacker), "config", "user.name", "test"],
            check=True,
        )
        (attacker / "attacker.py").write_text("redirected\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(attacker), "add", "attacker.py"], check=True)
        subprocess.run(
            ["git", "-C", str(attacker), "commit", "-qm", "attacker"], check=True
        )
        monkeypatch.setenv("GIT_DIR", str(attacker / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(attacker))

        assert module._snapshot_git(git_repo, "rev-parse", "HEAD") == expected_head

    def test_snapshot_captures_worktree_state_and_excludes_ignored(
        self, git_repo: Path
    ) -> None:
        import subprocess

        (git_repo / "allowed.py").write_text("modified\n")
        (git_repo / "untracked.py").write_text("new file\n")
        (git_repo / ".gitignore").write_text("secret.txt\n")
        (git_repo / "secret.txt").write_text("do not review\n")

        snapshot = module.create_review_snapshot(git_repo, "task/one")
        try:
            assert (snapshot.directory / "allowed.py").read_text() == "modified\n"
            assert (snapshot.directory / "untracked.py").exists()
            assert not (snapshot.directory / "secret.txt").exists()
            ref_sha = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(git_repo),
                    "rev-parse",
                    "refs/dispatch-snapshots/task_one",
                ],
                text=True,
            ).strip()
            assert ref_sha == snapshot.commit_sha
        finally:
            module.cleanup_review_snapshot(git_repo, snapshot)

    def test_snapshot_identity_is_deterministic_and_worktree_untouched(
        self, git_repo: Path
    ) -> None:
        import subprocess

        (git_repo / "allowed.py").write_text("modified\n")
        status_before = subprocess.check_output(
            ["git", "-C", str(git_repo), "status", "--porcelain"], text=True
        )

        first = module.create_review_snapshot(git_repo, "task-a")
        module.cleanup_review_snapshot(git_repo, first)
        second = module.create_review_snapshot(git_repo, "task-b")
        module.cleanup_review_snapshot(git_repo, second)

        assert first.commit_sha == second.commit_sha
        assert first.tree_sha == second.tree_sha
        status_after = subprocess.check_output(
            ["git", "-C", str(git_repo), "status", "--porcelain"], text=True
        )
        # Snapshotting must not stage or mutate the live worktree.
        assert status_after == status_before

    def test_cleanup_removes_checkout_but_keeps_provenance_ref(
        self, git_repo: Path
    ) -> None:
        import subprocess

        snapshot = module.create_review_snapshot(git_repo, "task-c")
        assert snapshot.directory.exists()

        module.cleanup_review_snapshot(git_repo, snapshot)

        assert not snapshot.directory.exists()
        worktrees = subprocess.check_output(
            ["git", "-C", str(git_repo), "worktree", "list", "--porcelain"],
            text=True,
        )
        assert str(snapshot.directory) not in worktrees
        ref_sha = subprocess.check_output(
            [
                "git",
                "-C",
                str(git_repo),
                "rev-parse",
                "refs/dispatch-snapshots/task-c",
            ],
            text=True,
        ).strip()
        assert ref_sha == snapshot.commit_sha

class TestFindingLedger:
    def _result_with_findings(self, findings: list[dict[str, object]]):
        return {**governed_result("review-task"), "findings": findings}

    @pytest.mark.parametrize(
        ("intent", "severity", "deposited"),
        [
            ("discovery", "suggestion", False),
            ("delivery-code-review", "suggestion", False),
            ("trust-manifest-verification", "important", False),
            ("trust-manifest-verification", "critical", False),
            ("discovery", "important", True),
            ("delivery-code-review", "important", True),
            ("resolution-adjudication", "critical", True),
            ("unknown-intent", "important", False),
        ],
    )
    def test_governed_deposit_allows_only_material_review_findings(
        self,
        git_repo: Path,
        tmp_path: Path,
        intent: str,
        severity: str,
        deposited: bool,
    ) -> None:
        repo = tmp_path / "primary"
        repo.mkdir()
        finding = {"severity": severity, "claim": "preserved review observation"}
        result = self._result_with_findings([finding])
        result["verification_verdict"] = "pass"
        receipt = module.append_finding_records(
            repo,
            task_id="intake-task",
            result=result,
            snapshot=None,
            worktree=git_repo,
            advisory=False,
            source_head="a" * 40,
            review_intent=intent,
        )
        assert result["findings"] == [finding]
        assert result["verification_verdict"] == "pass"
        assert (receipt is not None) is deposited
        records = module.load_finding_records(repo)
        assert len(records) == int(deposited)
        if deposited:
            assert records[0]["severity"] == severity
            assert records[0]["state"] == "open"
        else:
            assert not (repo / ".audit" / "findings").exists()

    def test_persisted_review_intents_track_the_review_intent_enum(self) -> None:
        from loopzero.review import evidence as dispatch_review_evidence

        assert dispatch_review_evidence.PERSISTED_REVIEW_INTENTS == (
            frozenset(module.REVIEW_INTENTS) - {"trust-manifest-verification"}
        )
        assert "trust-manifest-verification" in module.REVIEW_INTENTS

    def test_writer_anchors_findings_and_is_idempotent(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        repo = tmp_path / "primary"
        repo.mkdir()
        (git_repo / "allowed.py").write_text("line1\nline2\nline3\n")
        snapshot = module.create_review_snapshot(git_repo, "ledger-task")
        try:
            result = self._result_with_findings(
                [
                    {
                        "severity": "critical",
                        "claim": "off-by-one",
                        "path": "allowed.py",
                        "line_start": 2,
                        "line_end": 2,
                    },
                    {"severity": "suggestion", "claim": "cross-cutting nit"},
                ]
            )
            written = module.append_finding_records(
                repo,
                task_id="ledger-task",
                result=result,
                snapshot=snapshot,
                worktree=git_repo,
                advisory=False,
                source_head=None,
            )
            assert written["write_count"] == 1
            again = module.append_finding_records(
                repo,
                task_id="ledger-task",
                result=result,
                snapshot=snapshot,
                worktree=git_repo,
                advisory=False,
                source_head=None,
            )
            assert again == written

            records = module.load_finding_records(repo)
            assert len(records) == 1
            anchored = next(r for r in records if r["severity"] == "critical")
            assert anchored["state"] == "open"
            assert anchored["snapshot_tree_sha"] == snapshot.tree_sha
            assert anchored["anchor"]["path"] == "allowed.py"
            assert anchored["anchor"]["line_span"] == [2, 2]
            assert len(anchored["anchor"]["blob_sha"]) == 40
            assert "hunk_context_sha" in anchored["anchor"]
            assert result["findings"][1]["severity"] == "suggestion"
        finally:
            module.cleanup_review_snapshot(git_repo, snapshot)

    def test_formal_finding_promotes_a_matching_local_advisory_record(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snapshot = module.create_review_snapshot(
            git_repo, "promotion", ref_namespace="finding-snapshots"
        )
        finding = {
            "severity": "important",
            "claim": "The same defect was found locally and formally.",
            "path": "allowed.py",
        }
        try:
            advisory_receipt = module.append_finding_records(
                git_repo,
                task_id="local-review",
                result={"findings": [finding]},
                snapshot=snapshot,
                worktree=git_repo,
                advisory=True,
                source_head=None,
                producer_skill="review",
                category="correctness",
            )
            assert advisory_receipt["write_count"] == 1
            [advisory_record] = module.load_finding_records(git_repo)
            ledger_path = next((git_repo / module.FINDINGS_DIR).glob("*.jsonl"))
            ledger_path.write_text(
                ledger_path.read_text(encoding="utf-8")
                + json.dumps(
                    {
                        **advisory_record,
                        "severity": "suggestion",
                        "original_severity": "important",
                        "severity_correction": {
                            "from": "important",
                            "to": "suggestion",
                            "decision_record": "docs/decisions.md#stale",
                        },
                        "state": "addressed",
                        "disposition": "verified-locally",
                        "waiver_reason": "superseded by the prior local review",
                        "waiver_decision_record": "docs/decisions.md#waiver",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            formal_receipt = module.append_finding_records(
                git_repo,
                task_id="formal-review",
                result={"findings": [finding]},
                snapshot=snapshot,
                worktree=git_repo,
                advisory=False,
                source_head=None,
                producer_skill="review",
                category="correctness",
            )
            assert formal_receipt["write_count"] == 1

            [record] = module.load_finding_records(git_repo)
            assert record["review_task_id"] == "formal-review"
            assert record["advisory"] is None
            assert record["producer_skill"] == "review"
            assert record["category"] == "correctness"
            assert record["state"] == "open"
            assert record["disposition"] is None
            assert record["severity"] == "important"
            assert "fix_commit" not in record
            assert record["waiver_reason"] == "superseded by the prior local review"
            assert record["waiver_decision_record"] == "docs/decisions.md#waiver"
            assert record["original_severity"] == "important"
            assert record["severity_correction"] == {
                "from": "important",
                "to": "suggestion",
                "decision_record": "docs/decisions.md#stale",
            }
            assert record["capture_record_kind"] == "promotion"
            assert record["capture_operation_id"] == formal_receipt["operation_id"]
        finally:
            module.cleanup_review_snapshot(git_repo, snapshot)

    def test_formal_promotion_cannot_demote_or_reuse_advisory_operation(
        self, git_repo: Path
    ) -> None:
        snapshot = module.create_review_snapshot(
            git_repo, "promotion-authority", ref_namespace="finding-snapshots"
        )
        advisory_finding = {
            "severity": "critical",
            "claim": "The same defect must retain its original closure authority.",
            "path": "allowed.py",
        }
        formal_finding = {**advisory_finding, "severity": "important"}
        try:
            advisory_receipt = module.append_finding_records(
                git_repo,
                task_id="local-critical-review",
                result={"findings": [advisory_finding]},
                snapshot=snapshot,
                worktree=git_repo,
                advisory=True,
                source_head=None,
            )
            assert advisory_receipt["write_count"] == 1
            [advisory_record] = module.load_finding_records(git_repo)
            ledger_path = next((git_repo / module.FINDINGS_DIR).glob("*.jsonl"))
            ledger_path.write_text(
                ledger_path.read_text(encoding="utf-8")
                + json.dumps(
                    {
                        **advisory_record,
                        "operation_id": "op-stale",
                        "operation_request_digest": "digest-stale",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            formal_receipt = module.append_finding_records(
                git_repo,
                task_id="formal-important-review",
                result={"findings": [formal_finding]},
                snapshot=snapshot,
                worktree=git_repo,
                advisory=False,
                source_head=None,
            )
            assert formal_receipt["write_count"] == 1

            [record] = module.load_finding_records(git_repo)
            assert record["severity"] == "critical"
            assert record["capture_operation_id"] == formal_receipt["operation_id"]
        finally:
            module.cleanup_review_snapshot(git_repo, snapshot)

    def test_local_capture_is_anchored_advisory_and_idempotent(
        self, git_repo: Path
    ) -> None:
        (git_repo / ".git" / "info" / "exclude").write_text(
            ".audit/\n", encoding="utf-8"
        )
        findings = [
            {
                "severity": "important",
                "claim": "The fallback path can skip validation.",
                "path": "allowed.py",
                "line_start": 1,
                "line_end": 1,
            }
        ]

        written = module.record_local_findings(
            worktree=git_repo,
            review_id="local-review",
            producer_skill="code-quality-drift",
            category="maintainability",
            findings=findings,
        )
        again = module.record_local_findings(
            worktree=git_repo,
            review_id="local-review-repeat",
            producer_skill="code-quality-drift",
            category="maintainability",
            findings=findings,
        )

        assert written["write_count"] == 1
        assert again["write_count"] == 0
        assert again["outcomes"][0]["status"] == "replayed"
        [record] = module.load_finding_records(git_repo)
        assert record["review_task_id"] == "local-review"
        assert record["producer_skill"] == "code-quality-drift"
        assert record["category"] == "maintainability"
        assert record["advisory"] is True
        assert record["anchor"]["path"] == "allowed.py"
        import subprocess

        local_ref = subprocess.run(
            [
                "git",
                "-C",
                str(git_repo),
                "rev-parse",
                f"refs/finding-snapshots/local-review-{record['snapshot_tree_sha'][:12]}",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert local_ref == record["snapshot_sha"]
        formal_ref = subprocess.run(
            [
                "git",
                "-C",
                str(git_repo),
                "show-ref",
                "--verify",
                "refs/dispatch-snapshots/local-review",
            ],
            capture_output=True,
            text=True,
        )
        assert formal_ref.returncode != 0

    def test_local_capture_replays_before_snapshot_after_live_tree_drift(
        self, git_repo: Path
    ) -> None:
        import subprocess

        findings = [
            {
                "severity": "important",
                "claim": "The fallback path can skip validation.",
                "path": "allowed.py",
                "line_start": 1,
                "line_end": 1,
            }
        ]
        first = module.record_local_findings(
            worktree=git_repo,
            review_id="stable-local-review",
            producer_skill="code-quality-drift",
            category="maintainability",
            findings=findings,
        )
        (git_repo / "allowed.py").write_text("changed after review\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(git_repo), "add", "allowed.py"], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-qm", "move live tree"],
            check=True,
        )

        replay = module.record_local_findings(
            worktree=git_repo,
            review_id="stable-local-review",
            producer_skill="code-quality-drift",
            category="maintainability",
            findings=findings,
        )

        assert replay == first
        [record] = module.load_finding_records(git_repo)
        assert (
            record["snapshot_tree_sha"] == first["source_identity"]["snapshot_tree_sha"]
        )

    def test_local_capture_rejects_changed_result_for_same_review_id(
        self, git_repo: Path
    ) -> None:
        first = [
            {
                "severity": "important",
                "claim": "The fallback path can skip validation.",
                "path": "allowed.py",
            }
        ]
        module.record_local_findings(
            worktree=git_repo,
            review_id="conflicting-local-review",
            producer_skill="code-quality-drift",
            category="maintainability",
            findings=first,
        )

        with pytest.raises(module.FindingLedgerConflict, match="different payload"):
            module.record_local_findings(
                worktree=git_repo,
                review_id="conflicting-local-review",
                producer_skill="code-quality-drift",
                category="maintainability",
                findings=[{**first[0], "claim": "A changed producer result."}],
            )

    def test_local_capture_rejects_unanchorable_findings_before_write(
        self, git_repo: Path
    ) -> None:
        with pytest.raises(module.DispatchError, match="must anchor"):
            module.record_local_findings(
                worktree=git_repo,
                review_id="bad-local-review",
                producer_skill="audit-surface",
                category="correctness",
                findings=[
                    {
                        "severity": "critical",
                        "claim": "Missing file cannot support this claim.",
                        "path": "missing.py",
                    }
                ],
            )

        assert module.load_finding_records(git_repo) == []

    def _governed_capture_terminal(
        self,
        git_repo: Path,
        *,
        task_id: str = "dispatch-governed-fold-in",
        unit_attempt_number: int = 1,
        findings: list[dict[str, object]] | None = None,
        require_receipt: bool = True,
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        selected_findings = findings or [
            {
                "severity": "important",
                "claim": f"governed finding {index}",
                "path": "allowed.py",
                "line_start": 1,
                "line_end": 1,
            }
            for index in range(1, 6)
        ]
        result = {
            **self._result_with_findings(selected_findings),
            "task_id": task_id,
        }
        snapshot = module.ReviewSnapshot(
            commit_sha=module._snapshot_git(git_repo, "rev-parse", "HEAD"),
            tree_sha=module._snapshot_git(git_repo, "rev-parse", "HEAD^{tree}"),
            directory=git_repo,
        )
        source = module.source_identity(git_repo)
        result_artifact, result_sha256 = module.persist_result_artifact(
            git_repo, task_id=task_id, result=result
        )
        receipt = module.append_finding_records(
            git_repo,
            task_id=task_id,
            result=result,
            snapshot=snapshot,
            worktree=git_repo,
            advisory=False,
            source_head=str(source["head"]),
            producer_skill="governed-review",
            category="code-review",
            producer_kind="governed-review",
            unit_attempt_number=unit_attempt_number,
        )
        if require_receipt:
            assert receipt is not None
        capture_fields = (
            {"finding_capture_receipt": receipt} if receipt is not None else {}
        )
        terminal = governed(
            [
                {
                    "type": "attempt-terminal",
                    "task_id": task_id,
                    "work_unit_id": "issue-3614-review",
                    "unit_attempt_number": unit_attempt_number,
                    "status": "completed",
                    "work_kind": "review",
                    "category": "code-review",
                    "review_intent": "delivery-code-review",
                    "review_lens": "code",
                    "read_only": True,
                    "advisory": None,
                    "worker_identity": "claude:claude-opus-5",
                    "alias": "opus",
                    "effective_alias": "opus",
                    "worktree": str(git_repo),
                    "source_identity": source,
                    "snapshot_sha": snapshot.commit_sha,
                    "snapshot_tree_sha": snapshot.tree_sha,
                    "patch_identity": snapshot.patch_identity,
                    "result_artifact": result_artifact,
                    "result_sha256": result_sha256,
                    **capture_fields,
                    "acceptance_commands": [],
                }
            ]
        )[0]
        return terminal, receipt

    def test_governed_finding_fold_in_reuses_five_finding_receipt_without_writes(
        self, monkeypatch: pytest.MonkeyPatch, git_repo: Path
    ) -> None:
        terminal, receipt = self._governed_capture_terminal(git_repo)
        records = [terminal]
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: records
        )
        before = module.load_finding_records(git_repo)

        first = module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id=str(terminal["task_id"]),
            unit_attempt_number=1,
        )
        replay = module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id=str(terminal["task_id"]),
            unit_attempt_number=1,
        )

        assert first == replay
        assert first["receipt"] == receipt
        assert first["finding_ids"] == receipt["finding_ids"]
        assert len(first["finding_ids"]) == 5
        assert module.load_finding_records(git_repo) == before
        assert not [
            record for record in before if record.get("producer_kind") == "local-review"
        ]

    def test_governed_finding_fold_in_accepts_reopened_capture_outcome(
        self, monkeypatch: pytest.MonkeyPatch, git_repo: Path
    ) -> None:
        terminal, receipt = self._governed_capture_terminal(git_repo)
        reopened_receipt = json.loads(json.dumps(receipt))
        reopened_receipt["outcomes"][0]["status"] = "reopened"
        terminal = {**terminal, "finding_capture_receipt": reopened_receipt}
        _setattr_dispatch(
            monkeypatch,
            "latest_accepted_review_terminal",
            lambda _records, _task_id: terminal,
        )
        _setattr_dispatch(
            monkeypatch,
            "replay_finding_capture",
            lambda _repo, *, request: reopened_receipt,
        )

        folded = module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id=str(terminal["task_id"]),
            unit_attempt_number=1,
        )

        assert folded["receipt"] == reopened_receipt

    @pytest.mark.parametrize(
        ("terminal_update", "extra_records"),
        [
            ({"status": "failed"}, []),
            ({"status": "blocked"}, []),
            ({"advisory": True}, []),
            (
                {},
                [
                    {
                        "type": "attempt-supersession",
                        "task_id": "dispatch-governed-fold-in",
                        "status": "superseded",
                    }
                ],
            ),
        ],
    )
    def test_governed_finding_fold_in_rejects_unaccepted_terminals(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git_repo: Path,
        terminal_update: dict[str, object],
        extra_records: list[dict[str, object]],
    ) -> None:
        terminal, _receipt = self._governed_capture_terminal(git_repo)
        terminal.update(terminal_update)
        records = governed([terminal, *extra_records])
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: records
        )

        with pytest.raises(module.DispatchError, match="not accepted"):
            module.fold_in_governed_review_findings(
                worktree=git_repo,
                task_id="dispatch-governed-fold-in",
                unit_attempt_number=1,
            )

    def test_governed_finding_fold_in_rejects_missing_and_different_attempts(
        self, monkeypatch: pytest.MonkeyPatch, git_repo: Path
    ) -> None:
        terminal, _receipt = self._governed_capture_terminal(
            git_repo, unit_attempt_number=2
        )
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [terminal]
        )
        with pytest.raises(module.DispatchError, match="attempt"):
            module.fold_in_governed_review_findings(
                worktree=git_repo,
                task_id="dispatch-governed-fold-in",
                unit_attempt_number=1,
            )

        _setattr_dispatch(monkeypatch, "load_authority_records", lambda *_args: [])
        with pytest.raises(module.DispatchError, match="not accepted"):
            module.fold_in_governed_review_findings(
                worktree=git_repo,
                task_id="dispatch-missing",
                unit_attempt_number=1,
            )

        _setattr_dispatch(
            monkeypatch,
            "load_authority_records",
            lambda *_args: [
                {
                    key: value
                    for key, value in terminal.items()
                    if key not in {"schema_version", "policy_version"}
                }
            ],
        )
        with pytest.raises(module.DispatchError, match="not accepted"):
            module.fold_in_governed_review_findings(
                worktree=git_repo,
                task_id="dispatch-governed-fold-in",
                unit_attempt_number=2,
            )

    @pytest.mark.parametrize(
        "receipt_field",
        [
            "operation_request_digest",
            "result_digest",
            "source_identity",
            "advisory",
            "finding_ids",
        ],
    )
    def test_governed_finding_fold_in_rejects_tampered_receipt_evidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git_repo: Path,
        receipt_field: str,
    ) -> None:
        terminal, _receipt = self._governed_capture_terminal(git_repo)
        tampered = json.loads(json.dumps(terminal))
        receipt = tampered["finding_capture_receipt"]
        assert isinstance(receipt, dict)
        if receipt_field == "source_identity":
            receipt[receipt_field] = {"snapshot_sha": "f" * 40}
        elif receipt_field == "advisory":
            receipt[receipt_field] = True
        elif receipt_field == "finding_ids":
            receipt[receipt_field] = list(reversed(receipt[receipt_field]))
        else:
            receipt[receipt_field] = "f" * 64
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [tampered]
        )

        with pytest.raises(module.DispatchError, match="capture receipt"):
            module.fold_in_governed_review_findings(
                worktree=git_repo,
                task_id="dispatch-governed-fold-in",
                unit_attempt_number=1,
            )

    def test_governed_finding_fold_in_rejects_tampered_result_artifact(
        self, monkeypatch: pytest.MonkeyPatch, git_repo: Path
    ) -> None:
        terminal, _receipt = self._governed_capture_terminal(git_repo)
        (git_repo / str(terminal["result_artifact"])).write_text(
            '{"findings": []}\n', encoding="utf-8"
        )
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [terminal]
        )

        with pytest.raises(module.DispatchError, match="result artifact digest"):
            module.fold_in_governed_review_findings(
                worktree=git_repo,
                task_id="dispatch-governed-fold-in",
                unit_attempt_number=1,
            )

    @pytest.mark.parametrize("mutation", ["missing", "corrupted"])
    def test_governed_finding_fold_in_rejects_invalid_durable_members(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git_repo: Path,
        mutation: str,
    ) -> None:
        terminal, receipt = self._governed_capture_terminal(git_repo)
        finding_id = str(receipt["finding_ids"][0])
        ledger_path = next((git_repo / ".audit" / "findings").glob("*.jsonl"))
        records = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        member = next(
            record for record in records if record.get("finding_id") == finding_id
        )
        if mutation == "missing":
            records.remove(member)
        else:
            member["claim"] = "corrupted durable claim"
        ledger_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [terminal]
        )

        with pytest.raises(module.DispatchError, match="durable finding"):
            module.fold_in_governed_review_findings(
                worktree=git_repo,
                task_id="dispatch-governed-fold-in",
                unit_attempt_number=1,
            )

    def test_governed_finding_fold_in_preserves_best_effort_null_anchor(
        self, monkeypatch: pytest.MonkeyPatch, git_repo: Path
    ) -> None:
        terminal, receipt = self._governed_capture_terminal(git_repo)
        finding_id = str(receipt["finding_ids"][0])
        ledger_path = next((git_repo / ".audit" / "findings").glob("*.jsonl"))
        records = [json.loads(line) for line in ledger_path.read_text().splitlines()]
        member = next(
            record for record in records if record.get("finding_id") == finding_id
        )
        member["anchor"] = None
        ledger_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [terminal]
        )

        folded = module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id="dispatch-governed-fold-in",
            unit_attempt_number=1,
        )

        assert folded["finding_ids"] == receipt["finding_ids"]

    def test_governed_finding_fold_in_preserves_historical_suggestion_receipts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git_repo: Path,
    ) -> None:
        from loopzero.review import evidence as dispatch_review_evidence

        with monkeypatch.context() as legacy:
            legacy.setattr(
                dispatch_review_evidence,
                "persisted_review_findings",
                lambda findings, **_kwargs: findings,
            )
            terminal, receipt = self._governed_capture_terminal(
                git_repo,
                findings=[
                    {
                        "severity": "suggestion",
                        "claim": "historical nit",
                        "path": "allowed.py",
                    },
                ],
            )
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [terminal]
        )
        folded = module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id=str(terminal["task_id"]),
            unit_attempt_number=1,
        )
        assert folded["finding_ids"] == receipt["finding_ids"]
        assert len(folded["finding_ids"]) == 1

    def test_governed_finding_fold_in_returns_no_ids_for_suggestion_only_reviews(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git_repo: Path,
    ) -> None:
        terminal, receipt = self._governed_capture_terminal(
            git_repo,
            findings=[
                {"severity": "suggestion", "claim": "nit", "path": "allowed.py"},
            ],
            require_receipt=False,
        )
        assert receipt is None
        assert "finding_capture_receipt" not in terminal
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [terminal]
        )
        folded = module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id=str(terminal["task_id"]),
            unit_attempt_number=1,
        )
        assert folded["status"] == "folded-in"
        assert folded["finding_ids"] == []
        assert folded["receipt"] is None

    def test_governed_finding_fold_in_rejects_missing_receipt_for_material_findings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git_repo: Path,
    ) -> None:
        terminal, receipt = self._governed_capture_terminal(git_repo)
        assert receipt is not None
        receiptless = {
            key: value
            for key, value in terminal.items()
            if key != "finding_capture_receipt"
        }
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [receiptless]
        )
        with pytest.raises(module.DispatchError, match="capture receipt is missing"):
            module.fold_in_governed_review_findings(
                worktree=git_repo,
                task_id=str(terminal["task_id"]),
                unit_attempt_number=1,
            )

    def test_governed_finding_fold_in_preserves_mixed_result_evidence(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git_repo: Path,
    ) -> None:
        terminal, receipt = self._governed_capture_terminal(
            git_repo,
            findings=[
                {"severity": "important", "claim": "defect", "path": "allowed.py"},
                {"severity": "suggestion", "claim": "nit", "path": "allowed.py"},
            ],
        )
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [terminal]
        )
        folded = module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id=str(terminal["task_id"]),
            unit_attempt_number=1,
        )
        assert folded["finding_ids"] == receipt["finding_ids"]
        assert len(folded["finding_ids"]) == 1

    def test_governed_finding_fold_in_accepts_legitimate_lower_severity_replay(
        self, monkeypatch: pytest.MonkeyPatch, git_repo: Path
    ) -> None:
        finding = {
            "severity": "important",
            "claim": "same finding with a later severity assessment",
            "path": "allowed.py",
            "line_start": 1,
            "line_end": 1,
        }
        self._governed_capture_terminal(
            git_repo,
            task_id="dispatch-earlier-severity",
            findings=[finding],
        )
        terminal, receipt = self._governed_capture_terminal(
            git_repo,
            task_id="dispatch-later-severity",
            findings=[{**finding, "severity": "critical"}],
        )
        assert receipt["outcomes"] == [
            {"finding_id": receipt["finding_ids"][0], "status": "replayed"}
        ]
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [terminal]
        )

        folded = module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id="dispatch-later-severity",
            unit_attempt_number=1,
        )

        assert folded["finding_ids"] == receipt["finding_ids"]

    def test_governed_finding_fold_in_validates_recovered_semantic_result(
        self, monkeypatch: pytest.MonkeyPatch, git_repo: Path
    ) -> None:
        terminal, receipt = self._governed_capture_terminal(git_repo)
        terminal.update(
            {
                "attempt_index": 0,
                "run_id": "sr_" + "8" * 32,
                "output_identity": terminal["source_identity"],
            }
        )
        verification = {
            "type": "review-recovery-verification",
            "task_id": terminal["task_id"],
            "task_contract_hash": terminal["task_contract_hash"],
            "source_identity": terminal["source_identity"],
            "snapshot_sha": terminal["snapshot_sha"],
            "snapshot_tree_sha": terminal["snapshot_tree_sha"],
            "result_artifact": terminal["result_artifact"],
            "result_sha256": terminal["result_sha256"],
            "semantic_verdict": "pass",
            "blocker_class": "acceptance-receipt-only",
            "unresolved_findings": 0,
            "notes": "independent semantic verification passed",
            "verifier_identity": "codex:gpt-5.6-sol",
            "verifier_alias": "sol",
        }
        recovery = {
            **terminal,
            "type": "attempt-recovery",
            "result_artifact": "validation-only.json",
            "result_sha256": "e" * 64,
            "recovered_result_artifact": terminal["result_artifact"],
            "recovered_result_sha256": terminal["result_sha256"],
            "recovered_terminal_status": "completed",
            "recovery_classification": "acceptance-receipt-only",
            "recovery_verification": verification,
            "finding_capture_receipt": receipt,
        }
        _setattr_dispatch(
            monkeypatch,
            "load_authority_records",
            lambda _repo, _days: [terminal, recovery],
        )

        folded = module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id="dispatch-governed-fold-in",
            unit_attempt_number=1,
        )
        assert folded["receipt"] == receipt

        recovery["recovery_verification"] = {
            **verification,
            "semantic_verdict": "fail",
        }
        with pytest.raises(module.DispatchError, match="not accepted"):
            module.fold_in_governed_review_findings(
                worktree=git_repo,
                task_id="dispatch-governed-fold-in",
                unit_attempt_number=1,
            )

    def test_governed_fold_in_preserves_independent_local_review_capture(
        self, monkeypatch: pytest.MonkeyPatch, git_repo: Path
    ) -> None:
        terminal, _receipt = self._governed_capture_terminal(git_repo)
        _setattr_dispatch(
            monkeypatch, "load_authority_records", lambda _repo, _days: [terminal]
        )
        module.fold_in_governed_review_findings(
            worktree=git_repo,
            task_id="dispatch-governed-fold-in",
            unit_attempt_number=1,
        )

        local = module.record_local_findings(
            worktree=git_repo,
            review_id="independent-local-review",
            producer_skill="review",
            category="correctness",
            findings=[
                {
                    "severity": "suggestion",
                    "claim": "an independent defect class",
                    "path": "allowed.py",
                }
            ],
        )

        assert local["producer_kind"] == "local-review"
        assert len(module.load_finding_records(git_repo)) == 6

    def test_reader_latest_record_per_finding_wins(self, tmp_path: Path) -> None:
        directory = tmp_path / ".audit" / "findings"
        directory.mkdir(parents=True)
        (directory / "2026-08-01.jsonl").write_text(
            json.dumps({"finding_id": "f_1", "state": "open"}) + "\n"
        )
        (directory / "2026-08-02.jsonl").write_text(
            json.dumps({"finding_id": "f_1", "state": "addressed"}) + "\n"
        )

        records = module.load_finding_records(tmp_path)

        assert len(records) == 1
        assert records[0]["state"] == "addressed"

class TestDeltaReview:
    @pytest.fixture
    def byte_cap_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _setattr_dispatch(
            monkeypatch, "system_executable", lambda _name: Path("/usr/bin/git")
        )
        monkeypatch.setattr(
            module.patch_identity,
            "system_executable",
            lambda _name: Path("/usr/bin/git"),
        )

    def _tracked_audit_file(self, repo: Path) -> Path:
        path = repo / ".audit" / "tracked.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text("baseline\n", encoding="utf-8")
        with (repo / ".git" / "info" / "exclude").open("a") as excludes:
            excludes.write("\n.audit/\n")
        subprocess.run(["git", "-C", str(repo), "add", "-f", str(path)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "tracked audit fixture"],
            check=True,
        )
        return path

    def _amend(self, repo: Path) -> None:
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "--amend", "--no-edit", "-q"],
            check=True,
        )

    def _commit_exact_delta_patch_bytes(
        self, repo: Path, *, delta_from: str, patch_bytes: int
    ) -> str:
        """Create one real Git text patch with an exact encoded byte length."""
        path = repo / "delta-byte-cap.txt"
        path.write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", path.name], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "delta byte fixture"],
            check=True,
        )
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        probe = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--no-ext-diff",
                "--binary",
                delta_from,
                head,
            ]
        )
        fixed_bytes = len(probe) - len(b"+x\n")
        content_bytes = patch_bytes - fixed_bytes - len(b"+\n")
        assert content_bytes > 0
        path.write_text("x" * content_bytes + "\n", encoding="utf-8")
        self._amend(repo)
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        actual = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--no-ext-diff",
                "--binary",
                delta_from,
                head,
            ]
        )
        assert len(actual) == patch_bytes
        return head

    def _verdict(self, task_id: str, verdict: str = "pass") -> dict[str, object]:
        return governed([{"type": "verdict", "task_id": task_id, "verdict": verdict}])[
            0
        ]

    def _terminal(
        self,
        snapshot,
        task_id: str,
        *,
        category: str = "code-review",
        source_ref: str = "refs/heads/feature/1-demo",
        alias: str = "opus",
        **extra,
    ) -> dict[str, object]:
        terminal = governed(
            [
                {
                    "type": "attempt-terminal",
                    "task_id": task_id,
                    "status": "completed",
                    "work_kind": "review",
                    "read_only": True,
                    "snapshot_sha": snapshot.commit_sha,
                    "snapshot_tree_sha": snapshot.tree_sha,
                    "patch_identity": snapshot.patch_identity,
                    "category": category,
                    "alias": alias,
                    "source_identity": {"ref": source_ref},
                    "task_contract": {
                        "task_id": task_id,
                        "category": category,
                        **extra,
                    },
                    "acceptance": [],
                }
            ]
        )[0]
        terminal["task_contract_hash"] = module.task_contract_hash(
            terminal["task_contract"]
        )
        return terminal

    def test_receiptless_delta_predecessor_requires_fresh_full_review(
        self, git_repo: Path
    ) -> None:
        full = module.create_review_snapshot(git_repo, "full-review")
        (git_repo / "allowed.py").write_text("delta change\n")
        delta = module.create_review_snapshot(git_repo, "delta-review")
        try:
            receiptless = self._terminal(
                full,
                "receiptless-full-review",
                acceptance_commands=["true"],
            )
            with pytest.raises(module.DispatchError, match="no completed"):
                module.validate_delta_review(
                    [receiptless],
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                )
        finally:
            module.cleanup_review_snapshot(git_repo, full)
            module.cleanup_review_snapshot(git_repo, delta)

    def test_delta_predecessor_requires_independent_passing_verdict(
        self, git_repo: Path
    ) -> None:
        full = module.create_review_snapshot(git_repo, "full-review")
        (git_repo / "allowed.py").write_text("delta change\n")
        delta = module.create_review_snapshot(git_repo, "delta-review")
        try:
            with pytest.raises(module.DispatchError, match="independent pass"):
                module.validate_delta_review(
                    [self._terminal(full, "full-review")],
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                )
        finally:
            module.cleanup_review_snapshot(git_repo, full)
            module.cleanup_review_snapshot(git_repo, delta)

    def test_delta_predecessor_rejects_unsigned_preterminal_verdict(
        self, git_repo: Path
    ) -> None:
        full = module.create_review_snapshot(git_repo, "full-review")
        (git_repo / "allowed.py").write_text("delta change\n")
        delta = module.create_review_snapshot(git_repo, "delta-review")
        try:
            run_id = "sr_" + "1" * 32
            signer = module.TerminalAuthority.generate()
            terminal = self._terminal(full, "full-review")
            terminal.update(
                attempt_index=0,
                run_id=run_id,
                work_unit_id="full-review-unit",
                worktree=str(git_repo),
                worker_identity="claude:claude-opus-5",
            )
            start = governed(
                [
                    {
                        "type": "attempt-start",
                        "task_id": "full-review",
                        "work_unit_id": "full-review-unit",
                        "attempt_index": 0,
                        "run_id": run_id,
                        "worktree": str(git_repo),
                        "terminal_authority": signer.registration(),
                    }
                ]
            )[0]
            terminal = signer.seal(terminal, authority_kind="dispatcher")
            forged = governed(
                [
                    {
                        "type": "verdict",
                        "task_id": "full-review",
                        "run_id": run_id,
                        "verdict": "pass",
                    }
                ]
            )[0]

            with pytest.raises(module.DispatchError, match="independent pass"):
                module.validate_delta_review(
                    [forged, start, terminal],
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                )
        finally:
            module.cleanup_review_snapshot(git_repo, full)
            module.cleanup_review_snapshot(git_repo, delta)

    @pytest.mark.parametrize(
        ("patch_bytes", "accepted"),
        [
            (module.DELTA_REVIEW_PATCH_MAX_BYTES - 1, True),
            (module.DELTA_REVIEW_PATCH_MAX_BYTES, True),
            (module.DELTA_REVIEW_PATCH_MAX_BYTES + 1, False),
        ],
    )
    def test_delta_review_real_git_patch_preserves_immutable_byte_boundary(
        self, git_repo: Path, byte_cap_git: None, patch_bytes: int, accepted: bool
    ) -> None:
        delta_from = subprocess.check_output(
            ["git", "-C", str(git_repo), "rev-parse", "HEAD"], text=True
        ).strip()
        current = self._commit_exact_delta_patch_bytes(
            git_repo, delta_from=delta_from, patch_bytes=patch_bytes
        )
        snapshot = SimpleNamespace(commit_sha=current)

        if accepted:
            patch = module.materialize_delta_review_patch(
                git_repo, delta_from=delta_from, new_snapshot=snapshot
            )
            assert len(patch.encode("utf-8")) == patch_bytes
        else:
            with pytest.raises(module.DispatchError, match="bounded prompt size"):
                module.materialize_delta_review_patch(
                    git_repo, delta_from=delta_from, new_snapshot=snapshot
                )

    @pytest.mark.parametrize("dirty_audit", [False, True])
    def test_uncarryable_delta_authority_uses_real_git_byte_cap_after_low_churn(
        self, git_repo: Path, byte_cap_git: None, dirty_audit: bool
    ) -> None:
        audit = self._tracked_audit_file(git_repo)
        source_ref = str(module.source_identity(git_repo)["ref"])
        predecessor = module.create_review_snapshot(git_repo, "byte-cap-authority")
        try:
            current = self._commit_exact_delta_patch_bytes(
                git_repo,
                delta_from=predecessor.commit_sha,
                patch_bytes=module.DELTA_REVIEW_PATCH_MAX_BYTES + 1,
            )
            terminal = self._terminal(
                predecessor,
                "byte-cap-authority",
                source_ref=source_ref,
            )
            if dirty_audit:
                audit.write_text("changed\n", encoding="utf-8")
            current_source = {**module.source_identity(git_repo), "head": current}
            authority = module.uncarryable_delta_authority(
                terminal,
                worktree=git_repo,
                current_source=current_source,
                history=[terminal, self._verdict("byte-cap-authority")],
            )

            if dirty_audit:
                assert authority is None
                return
            assert authority is not None
            assert authority["reason"] == "delta-byte-cap-exceeded"
            assert authority["delta_patch_bytes"] == (
                module.DELTA_REVIEW_PATCH_MAX_BYTES + 1
            )
            assert authority["delta_patch_max_bytes"] == (
                module.DELTA_REVIEW_PATCH_MAX_BYTES
            )
            assert authority["delta_patch_from_sha"] == predecessor.commit_sha
            assert authority["delta_patch_to_sha"] == current
            assert re.fullmatch(r"[0-9a-f]{64}", authority["delta_patch_sha256"])
            assert authority.get("cumulative_churn", 0) <= (
                module.DELTA_CUMULATIVE_LINE_CAP
            )
        finally:
            module.cleanup_review_snapshot(git_repo, predecessor)

    def test_delta_byte_read_failure_is_not_empty_success(
        self, git_repo: Path, byte_cap_git: None
    ) -> None:
        with pytest.raises(module.DispatchError, match="materialization failed"):
            module.materialize_delta_review_patch(
                git_repo,
                delta_from="f" * 40,
                new_snapshot=SimpleNamespace(commit_sha="HEAD"),
            )

    @pytest.mark.parametrize(
        "invalid", ["source", "predecessor", "unsigned", "git-object"]
    )
    def test_byte_cap_authority_rejects_invalid_source_or_predecessor(
        self, git_repo: Path, byte_cap_git: None, invalid: str
    ) -> None:
        predecessor = module.create_review_snapshot(git_repo, "byte-invalid")
        try:
            self._commit_exact_delta_patch_bytes(
                git_repo,
                delta_from=predecessor.commit_sha,
                patch_bytes=module.DELTA_REVIEW_PATCH_MAX_BYTES + 1,
            )
            source = module.source_identity(git_repo)
            terminal = self._terminal(
                predecessor, "byte-invalid", source_ref=str(source["ref"])
            )
            history = [terminal, self._verdict("byte-invalid")]
            target = dict(terminal)
            if invalid == "source":
                source = {**source, "state_sha256": "f" * 64}
            elif invalid == "predecessor":
                target["task_id"] = "wrong-predecessor"
            elif invalid == "unsigned":
                history = []
            else:
                target["snapshot_sha"] = "f" * 40
            assert (
                module.uncarryable_delta_authority(
                    target, worktree=git_repo, current_source=source, history=history
                )
                is None
            )
        finally:
            module.cleanup_review_snapshot(git_repo, predecessor)

    def test_verified_fail_delta_review_reroutes_on_same_tree(
        self, git_repo: Path
    ) -> None:
        full = module.create_review_snapshot(git_repo, "full-review")
        (git_repo / "allowed.py").write_text("failed delta\n")
        self._amend(git_repo)
        delta = module.create_review_snapshot(git_repo, "failed-delta-review")
        try:
            history = [
                self._terminal(full, "full-review"),
                self._verdict("full-review"),
                self._terminal(delta, "failed-delta-review"),
                governed(
                    [
                        {
                            "type": "verdict",
                            "task_id": "failed-delta-review",
                            "verdict": "fail",
                        }
                    ]
                )[0],
            ]

            assert (
                module.validate_delta_review(
                    history,
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                    alias="sol",
                )["delta_from_snapshot_sha"]
                == full.commit_sha
            )
            with pytest.raises(module.DispatchError, match="designated escalation"):
                module.validate_delta_review(
                    history,
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                    alias="fable",
                )
            with pytest.raises(module.DispatchError, match="designated escalation"):
                module.validate_delta_review(
                    history,
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                )
            with pytest.raises(module.DispatchError, match="designated escalation"):
                module.validate_delta_review(
                    history,
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                    alias="terra",
                )
            history[1]["alias"] = "terra"
            assert (
                module.validate_delta_review(
                    history,
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                    alias="sol",
                )["delta_from_snapshot_sha"]
                == full.commit_sha
            )
        finally:
            module.cleanup_review_snapshot(git_repo, full)
            module.cleanup_review_snapshot(git_repo, delta)

    def test_tree_identical_snapshot_with_new_commit_is_not_a_delta(
        self, git_repo: Path
    ) -> None:
        import subprocess

        (git_repo / "allowed.py").write_text("already reviewed\n")
        predecessor = module.create_review_snapshot(git_repo, "predecessor")
        subprocess.run(["git", "-C", str(git_repo), "add", "allowed.py"], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-qm", "reviewed state"],
            check=True,
        )
        same_tree = module.create_review_snapshot(git_repo, "same-tree")
        try:
            assert predecessor.commit_sha != same_tree.commit_sha
            assert predecessor.tree_sha == same_tree.tree_sha
            with pytest.raises(
                module.DispatchError, match="source tree has not advanced"
            ):
                module.validate_delta_review(
                    [
                        self._terminal(predecessor, "predecessor"),
                        self._verdict("predecessor"),
                    ],
                    delta_from=predecessor.commit_sha,
                    worktree=git_repo,
                    new_snapshot=same_tree,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                )
        finally:
            module.cleanup_review_snapshot(git_repo, predecessor)
            module.cleanup_review_snapshot(git_repo, same_tree)

    def test_delta_chain_projects_authenticated_supersessions_once(
        self, git_repo: Path, monkeypatch
    ) -> None:
        full = module.create_review_snapshot(git_repo, "full-review")
        try:
            retention = sys.modules["dispatch_authority_store"]._retention_state_record(
                [], live_record_ids=set()
            )
            records = [
                retention,
                self._terminal(full, "full-review"),
                self._verdict("full-review"),
                self._terminal(full, "unrelated-review"),
                self._verdict("unrelated-review"),
            ]
            history = module.AuthorityRecordView(
                records, trusted_retained_ids={id(retention)}
            )
            projection_calls = 0
            project = module.authenticated_supersessions

            def counted_projection(records, **kwargs):
                nonlocal projection_calls
                projection_calls += 1
                return project(records, **kwargs)

            _setattr_dispatch(
                monkeypatch, "authenticated_supersessions", counted_projection
            )
            _setattr_dispatch(
                monkeypatch,
                "latest_accepted_review_terminal",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("per-task accepted-terminal projection")
                ),
            )

            prior, predecessor = module._resolve_delta_chain(
                history,
                full.commit_sha,
                category="code-review",
                source_ref="refs/heads/feature/1-demo",
                new_tree_sha="f" * 40,
            )

            assert prior["snapshot_sha"] == full.commit_sha
            assert predecessor is prior
            assert projection_calls == 1
        finally:
            module.cleanup_review_snapshot(git_repo, full)

    def test_delta_chain_preserves_checkpoint_authenticated_history_view(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        full = module.create_review_snapshot(git_repo, "full-review")
        try:
            terminal = self._terminal(full, "full-review")
            verdict = self._verdict("full-review")
            history = module.AuthorityRecordView(
                [terminal, verdict],
                trusted_retained_ids={id(terminal), id(verdict)},
            )

            project = module.authenticated_supersessions

            def authenticated_projection(records, **kwargs):
                assert isinstance(records, module.AuthorityRecordView)
                assert records.trusted_retained_ids == {id(terminal), id(verdict)}
                return project(records, **kwargs)

            _setattr_dispatch(
                monkeypatch,
                "authenticated_supersessions",
                authenticated_projection,
            )

            prior, predecessor = module._resolve_delta_chain(
                history,
                full.commit_sha,
                category="code-review",
                source_ref="refs/heads/feature/1-demo",
                new_tree_sha="f" * 40,
            )

            assert prior is terminal
            assert predecessor is terminal
        finally:
            module.cleanup_review_snapshot(git_repo, full)

    def test_delta_chain_preserves_compacted_authority_provenance(
        self, git_repo: Path
    ) -> None:
        full = module.create_review_snapshot(git_repo, "full-review")
        (git_repo / "allowed.py").write_text("compacted delta\n")
        self._amend(git_repo)
        delta = module.create_review_snapshot(git_repo, "delta-review")
        try:
            terminal = self._terminal(full, "full-review")
            verdict = self._verdict("full-review")
            coordinator = module.create_coordinator_authority()
            checkpoint_prefix = {"scheme": "authority-ledger-accumulator-v3"}
            checkpoint = coordinator.seal(
                governed(
                    [
                        {
                            "type": "coordinator-authority-cutover",
                            "status": "active",
                            "ledger_prefix": checkpoint_prefix,
                        }
                    ]
                )[0],
                authority_kind="coordinator",
            )
            history = module.AuthorityRecordView(
                [checkpoint, terminal, verdict],
                trusted_checkpoint_id=id(checkpoint),
                trusted_retained_ids={id(terminal), id(verdict)},
                checkpoint_prefix=checkpoint_prefix,
            )

            edge = module.validate_delta_review(
                history,
                delta_from=full.commit_sha,
                worktree=git_repo,
                new_snapshot=delta,
                category="code-review",
                source_ref="refs/heads/feature/1-demo",
            )

            assert edge["delta_from_snapshot_sha"] == full.commit_sha
            with pytest.raises(module.DispatchError, match="no completed"):
                module.validate_delta_review(
                    list(history),
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                )
        finally:
            module.cleanup_review_snapshot(git_repo, full)
            module.cleanup_review_snapshot(git_repo, delta)

    def test_cumulative_churn_past_floor_refuses_delta(self, git_repo: Path) -> None:
        full = module.create_review_snapshot(git_repo, "full-review")
        (git_repo / "big.py").write_text("".join(f"line{i}\n" for i in range(400)))
        self._amend(git_repo)
        delta = module.create_review_snapshot(git_repo, "delta-review")
        try:
            with pytest.raises(module.DispatchError, match="cumulative delta churn"):
                module.validate_delta_review(
                    [
                        self._terminal(full, "full-review"),
                        self._verdict("full-review"),
                    ],
                    delta_from=full.commit_sha,
                    worktree=git_repo,
                    new_snapshot=delta,
                    category="code-review",
                    source_ref="refs/heads/feature/1-demo",
                )
        finally:
            module.cleanup_review_snapshot(git_repo, full)
            module.cleanup_review_snapshot(git_repo, delta)

    def test_security_fix_path_can_use_the_same_lens_delta(
        self, git_repo: Path
    ) -> None:
        full = module.create_review_snapshot(git_repo, "full-review")
        (git_repo / "pyproject.toml").write_text("[project]\nname='x'\n")
        self._amend(git_repo)
        delta = module.create_review_snapshot(git_repo, "delta-review")
        try:
            edge = module.validate_delta_review(
                [
                    self._terminal(full, "full-review", category="security"),
                    self._verdict("full-review"),
                ],
                delta_from=full.commit_sha,
                worktree=git_repo,
                new_snapshot=delta,
                category="security",
                source_ref="refs/heads/feature/1-demo",
            )
            assert edge["delta_from_snapshot_sha"] == full.commit_sha
        finally:
            module.cleanup_review_snapshot(git_repo, full)
            module.cleanup_review_snapshot(git_repo, delta)

class TestEvidenceSnapshot:
    def test_accepts_evidence_at_the_bounded_file_limit(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        assert module.EVIDENCE_MAX_FILES == 64

        evidence_paths = []
        for index in range(module.EVIDENCE_MAX_FILES):
            path = git_repo / f"evidence-{index}.txt"
            path.write_text(f"evidence {index}\n", encoding="utf-8")
            evidence_paths.append(str(path))

        resolved = module.validate_evidence_inputs(
            worktree=git_repo,
            primary_repo=tmp_path / "primary",
            evidence_paths=evidence_paths,
        )

        assert len(resolved) == module.EVIDENCE_MAX_FILES

    def test_rejects_evidence_above_the_bounded_file_limit(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        evidence_paths = []
        for index in range(module.EVIDENCE_MAX_FILES + 1):
            path = git_repo / f"evidence-{index}.txt"
            path.write_text(f"evidence {index}\n", encoding="utf-8")
            evidence_paths.append(str(path))

        with pytest.raises(module.DispatchError, match="file count exceeds limit"):
            module.validate_evidence_inputs(
                worktree=git_repo,
                primary_repo=tmp_path / "primary",
                evidence_paths=evidence_paths,
            )

    def test_stages_wide_merge_delta_path_manifest_as_one_evidence_file(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        evidence = git_repo / "merge-delta-paths.json"
        payload = {
            "schema_version": "delivery-merge-delta-paths-v1",
            "merge_delta_digest": "a" * 64,
            "paths": [
                {"path": f"src/path-{index}.py", "blob_sha": f"{index:040x}"}
                for index in range(21)
            ],
        }
        payload["paths"][-1]["blob_sha"] = None
        evidence.write_text(json.dumps(payload), encoding="utf-8")

        snapshot = module.stage_evidence_snapshot(
            worktree=git_repo,
            primary_repo=tmp_path / "primary",
            task_id="wide-merge-delta",
            evidence_paths=[str(evidence)],
        )

        assert len(snapshot.manifest) == 1
        assert snapshot.manifest[0]["delivery_merge_delta_manifest"] == payload
        module.cleanup_evidence_snapshot(snapshot)

    def test_stages_every_declared_input_with_manifest_and_permissions(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        local = git_repo / "ignored.audit"
        local.write_text("local evidence\n", encoding="utf-8")
        primary = tmp_path / "primary"
        audit = primary / ".audit" / "reports"
        audit.mkdir(parents=True)
        external = audit / "run.json"
        external.write_text('{"ok":true}\n', encoding="utf-8")

        snapshot = module.stage_evidence_snapshot(
            worktree=git_repo,
            primary_repo=primary,
            task_id="evidence-task",
            evidence_paths=[str(local), str(external)],
        )

        assert len(snapshot.manifest) == 2
        assert snapshot.directory.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o400 for path in snapshot.files)
        assert all(path.is_relative_to(git_repo / ".audit") for path in snapshot.files)
        staged_contents = {path.read_text(encoding="utf-8") for path in snapshot.files}
        assert staged_contents == {"local evidence\n", '{"ok":true}\n'}
        module.verify_evidence_snapshot(snapshot)
        module.cleanup_evidence_snapshot(snapshot)
        assert not snapshot.directory.exists()

    def test_mirrors_declared_evidence_into_immutable_review_checkout(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        (git_repo / ".git" / "info" / "exclude").write_text(
            ".audit/\n", encoding="utf-8"
        )
        evidence = git_repo / ".audit" / "review.diff"
        evidence.parent.mkdir()
        evidence.write_text("diff --git a/a b/a\n", encoding="utf-8")
        inputs = module.stage_evidence_snapshot(
            worktree=git_repo,
            primary_repo=tmp_path / "primary",
            task_id="evidence-review",
            evidence_paths=[str(evidence)],
        )
        review = module.create_review_snapshot(git_repo, "evidence-review")

        try:
            mirrored_snapshot = module.mirror_evidence_into_review_snapshot(
                worktree=git_repo,
                evidence_snapshot=inputs,
                review_snapshot=review,
            )

            mirrored = review.directory / str(inputs.manifest[0]["path"])
            assert mirrored.read_text(encoding="utf-8") == evidence.read_text(
                encoding="utf-8"
            )
            assert mirrored.stat().st_mode & 0o777 == 0o400
            module.verify_evidence_snapshot(inputs)
            module.verify_evidence_snapshot(mirrored_snapshot)

            mirrored.chmod(0o600)
            mirrored.write_text("tampered\n", encoding="utf-8")
            with pytest.raises(module.DispatchError, match="evidence-drift"):
                module.verify_evidence_snapshot(mirrored_snapshot)
        finally:
            module.cleanup_review_snapshot(git_repo, review)
            module.cleanup_evidence_snapshot(inputs)

    def test_module_avoids_python_314_only_path_copy(self) -> None:
        """Regression for #3000: `Path.copy` is 3.14-only; the dispatcher must
        stay runnable under system Python, so file copies go through shutil."""
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert not re.search(r"(?<!shutil)\.copy\(", source)

    @pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
    def test_rejects_unsupported_evidence_before_snapshot(
        self, git_repo: Path, tmp_path: Path, kind: str
    ) -> None:
        candidate = git_repo / kind
        if kind == "directory":
            candidate.mkdir()
        elif kind == "symlink":
            candidate.symlink_to(tmp_path / "outside")

        with pytest.raises(module.DispatchError, match="evidence-unavailable"):
            module.stage_evidence_snapshot(
                worktree=git_repo,
                primary_repo=tmp_path / "primary",
                task_id="bad-evidence",
                evidence_paths=[str(candidate)],
            )

    def test_rejects_external_file_outside_primary_audit(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        primary = tmp_path.parent / f"primary-{tmp_path.name}"
        outside = primary / "secret.txt"
        outside.parent.mkdir()
        outside.write_text("secret", encoding="utf-8")

        with pytest.raises(module.DispatchError, match="evidence-unavailable"):
            module.stage_evidence_snapshot(
                worktree=git_repo,
                primary_repo=primary,
                task_id="outside-evidence",
                evidence_paths=[str(outside)],
            )

    def test_snapshot_rehash_detects_mutation(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        source = git_repo / "evidence.txt"
        source.write_text("before", encoding="utf-8")
        snapshot = module.stage_evidence_snapshot(
            worktree=git_repo,
            primary_repo=tmp_path / "primary",
            task_id="drift-evidence",
            evidence_paths=[str(source)],
        )
        snapshot.files[0].chmod(0o600)
        snapshot.files[0].write_text("after", encoding="utf-8")

        with pytest.raises(module.DispatchError, match="evidence-drift"):
            module.verify_evidence_snapshot(snapshot)

        module.cleanup_evidence_snapshot(snapshot)

    def test_sweep_removes_only_old_marker_validated_snapshots(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        source = git_repo / "evidence.txt"
        source.write_text("evidence", encoding="utf-8")
        old = module.stage_evidence_snapshot(
            worktree=git_repo,
            primary_repo=tmp_path / "primary",
            task_id="old",
            evidence_paths=[str(source)],
        )
        recent = module.stage_evidence_snapshot(
            worktree=git_repo,
            primary_repo=tmp_path / "primary",
            task_id="recent",
            evidence_paths=[str(source)],
        )
        owner = old.directory / module.EVIDENCE_OWNER_FILENAME
        payload = json.loads(owner.read_text(encoding="utf-8"))
        payload["created_at"] = "2026-07-11T00:00:00+00:00"
        owner.chmod(0o600)
        owner.write_text(json.dumps(payload), encoding="utf-8")
        owner.chmod(0o400)
        unknown = old.directory.parent / "unknown"
        unknown.mkdir()
        (unknown / "keep.txt").write_text("keep", encoding="utf-8")

        outcomes = module.sweep_owned_evidence_snapshots(
            worktree=git_repo,
            all_owned=False,
            now=module.datetime(2026, 7, 13, tzinfo=module.UTC),
        )

        assert outcomes == [
            {
                "snapshot": old.directory.relative_to(git_repo).as_posix(),
                "status": "removed",
            }
        ]
        assert not old.directory.exists()
        assert recent.directory.exists()
        assert unknown.exists()
        module.cleanup_evidence_snapshot(recent)

    def test_all_owned_sweep_preserves_invalid_marker_and_unknown_content(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        source = git_repo / "evidence.txt"
        source.write_text("evidence", encoding="utf-8")
        owned = module.stage_evidence_snapshot(
            worktree=git_repo,
            primary_repo=tmp_path / "primary",
            task_id="owned",
            evidence_paths=[str(source)],
        )
        invalid = owned.directory.parent / "invalid"
        invalid.mkdir()
        (invalid / module.EVIDENCE_OWNER_FILENAME).write_text(
            '{"schema_version":"unknown"}', encoding="utf-8"
        )

        outcomes = module.sweep_owned_evidence_snapshots(
            worktree=git_repo,
            all_owned=True,
        )

        assert outcomes[0]["status"] == "removed"
        assert not owned.directory.exists()
        assert invalid.exists()
