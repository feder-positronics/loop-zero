"""Native v2 registration, result, bounded delta and readiness composition."""

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority as signing
from loopzero.kernel import authority_projection as projection
from loopzero.kernel import (
    authority_store,
    patch_identity,
    review_state,
    worktree_lease,
)
from loopzero.kernel.gitscope import ReviewSnapshot, task_contract_hash
from loopzero.review import admission, chain, evidence, pr_review
from loopzero.review.acceptance import build_review_acceptance_receipt
from loopzero.runners.contract import ReviewOutcome


def git(root, *args):
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=probe",
            "-c",
            "user.email=probe@example.invalid",
            *args,
        ],
        text=True,
    ).strip()


@pytest.fixture
def delivery(consumer, tmp_path, monkeypatch, isolated_ptrace_scope_path):
    monkeypatch.setattr(signing, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
    monkeypatch.setattr(
        signing, "_coordinator_state_directory", lambda: tmp_path / "keys"
    )
    evidence.configure(
        SimpleNamespace(
            root=consumer,
            audit_root=Path(".audit"),
            github=SimpleNamespace(ref_namespace="refs/heads"),
        )
    )
    monkeypatch.setattr(
        "loopzero.review.acceptance._toolchain",
        lambda: {"db_lock": "/tmp/test-db.lock"},
    )
    chain.configure(
        SimpleNamespace(
            required_sections=("code",), max_reviews_per_pr=1, max_delta_reviews=1
        )
    )
    # Consumer audit artifacts are ignored runtime state, as in real consumers.
    (consumer / ".git/info/exclude").write_text(".audit/\n")
    coordinator = authority_store.create_coordinator_authority()

    def seal(row):
        return coordinator.seal(
            row, authority_kind="coordinator", include_public_key=True
        )

    rows = [
        seal(
            {
                "type": "coordinator-authority-cutover",
                "status": "active",
                "ledger_prefix": projection.coordinator_ledger_prefix([]),
            }
        )
    ]
    base = git(consumer, "rev-parse", "HEAD")
    branch = git(consumer, "branch", "--show-current")
    git(consumer, "update-ref", "refs/remotes/origin/main", base)
    run = "sr_" + "3" * 32
    path = consumer / ".audit/skill-runs/runs.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run,
                "delivery_contract": "loop-zero-v2",
                "git_branch": branch,
                "outcome": "in_progress",
            }
        )
        + "\n"
    )
    published = []

    class Remote:
        def json(self, argv):
            if argv[-1] == "repos/{owner}/{repo}":
                return {"full_name": "org/repo"}
            if argv[-1].endswith("/reviews"):
                return [published]
            if "POST" in argv:
                fields = dict(arg.split("=", 1) for arg in argv if "=" in arg)
                published.append(
                    {
                        "body": fields["body"],
                        "commit_id": fields["commit_id"],
                        "state": "COMMENTED",
                    }
                )
                return {}
            return {
                "number": 7,
                "state": "open",
                "body": f"<!-- skill-run-id: {run} -->",
                "head": {
                    "sha": git(consumer, "rev-parse", "HEAD"),
                    "ref": branch,
                    "repo": {"full_name": "org/repo"},
                },
                "base": {"sha": base, "ref": "main", "repo": {"full_name": "org/repo"}},
            }

    remote = Remote()

    def source():
        return {
            "repository": "org/repo",
            "pr": 7,
            "run_id": run,
            "head": git(consumer, "rev-parse", "HEAD"),
            "branch": branch,
            "base": "main",
            "base_sha": base,
        }

    def change(value):
        (consumer / "owned.py").write_text(f"VALUE = {value}\n")
        git(consumer, "add", "owned.py")
        git(consumer, "commit", "-qm", "candidate")
        return patch_identity.capture_patch_identity(
            consumer, candidate_sha=git(consumer, "rev-parse", "HEAD"), base_ref=base
        )

    def review(name, identity, *, first=None, outcome="repaired", new_findings=()):
        task = {
            "task_id": name,
            "idempotency_key": name,
            "run_id": run,
            "work_kind": "review",
            "review_intent": "delivery-code-review",
            "security_trigger_paths": [],
            "pr_identity": source(),
            "acceptance_commands": ['python3 -c "assert True"'],
        }
        requested = "review"
        if first:
            requested = "delta"
            task["primary_review_task_id"] = first.slot.task_id
            link = review_state.GenerationLinkV1(
                repository_binding=first.generation.repository_binding,
                predecessor_generation_id=first.generation.generation_id,
                from_identity=first.generation.patch_identity,
                to_identity=identity,
                transition_kind="substantive",
            )
            rows.append(seal(link.to_dict()))
        with authority_store.authority_ledger_lock(consumer):
            admitted = admission.admit_review(
                consumer,
                rows,
                repository_binding=authority_store.authority_repository_binding(
                    consumer
                ),
                task=task,
                current_source_identity=worktree_lease.source_identity(consumer),
                source_worktree=consumer,
                current_tree_sha=identity["candidate_tree_sha"],
                patch_identity=identity,
                required_sections=("code",),
                equivalence_proof=None,
                format_only_proof=None,
                requested=requested,
                changed_paths=None,
                security_trigger_paths=(),
                pr_review_runner=remote,
            )
            assert isinstance(admitted, admission.Reserved), admitted
            rows.extend(seal(dict(row)) for row in admitted.records_to_append)
            task = dict(admitted.scoped_task)
            findings = (
                list(new_findings)
                if first
                else [
                    {
                        "severity": "important",
                        "claim": "Wrong value",
                        "path": "owned.py",
                    }
                ]
            )
            result = {
                "findings": findings,
                "review_sections": {
                    "code": {
                        "completion": "completed",
                        "verdict": "findings" if findings else "clean",
                        "findings": findings,
                    }
                },
            }
            if first:
                result.update(
                    primary_result_sha256=task["primary_result_sha256"],
                    dispositions=[
                        {
                            "finding_id": fid,
                            "outcome": outcome,
                            "rationale": "Checked candidate and acceptance.",
                        }
                        for fid in task["required_finding_ids"]
                    ],
                )
            artifact = consumer / f".audit/{name}.json"
            artifact.write_text(json.dumps(result))
            dispatcher = signing.TerminalAuthority.generate()
            snapshot = ReviewSnapshot(
                commit_sha=source()["head"],
                tree_sha=identity["candidate_tree_sha"],
                directory=consumer,
                patch_identity=identity,
            )
            execution = subprocess.run(
                ["python3", "-c", "assert True"],
                capture_output=True,
                text=True,
                check=False,
            )
            acceptance = build_review_acceptance_receipt(
                task=task,
                source_identity=worktree_lease.source_identity(consumer),
                snapshot=snapshot,
                worktree=consumer,
                provenance="pre-model",
                results=[
                    {
                        "command": task["acceptance_commands"][0],
                        "exit_code": execution.returncode,
                        "tail": execution.stdout,
                    }
                ],
            )
            common = {
                "schema_version": projection.TELEMETRY_SCHEMA_VERSION,
                "policy_version": next(
                    iter(projection.COMPATIBLE_DISPATCH_POLICY_VERSIONS)
                ),
                "task_id": name,
                "work_unit_id": name,
                "run_id": run,
                "attempt_index": 0,
                "worktree": str(consumer),
                "read_only": True,
                "work_kind": "review",
                "review_intent": "delivery-code-review",
                "task_contract": task,
                "task_contract_hash": task_contract_hash(task),
                "source_identity": worktree_lease.source_identity(consumer),
                "snapshot_sha": source()["head"],
                "snapshot_tree_sha": identity["candidate_tree_sha"],
                "patch_identity": identity,
                "review_generation_id": admitted.generation.generation_id,
                "review_reservation_id": admitted.slot.reservation_id,
                "result_artifact": f".audit/{name}.json",
                "result_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "acceptance_receipt": acceptance,
                "acceptance": [
                    {
                        "command": task["acceptance_commands"][0],
                        "exit_code": execution.returncode,
                    }
                ],
                "review_chain_receipt": chain.build_review_chain_receipt(
                    task=task,
                    snapshot_sha=source()["head"],
                    snapshot_tree_sha=identity["candidate_tree_sha"],
                    patch_identity=identity,
                    result=result,
                    finding_ids=list(
                        pr_review.material_findings(
                            identity["candidate_tree_sha"], result
                        )
                    ),
                ),
            }
            rows.append(
                seal(
                    {
                        **common,
                        "type": "attempt-start",
                        "registration_authority_version": 1,
                        "terminal_authority": dispatcher.registration(),
                    }
                )
            )
            terminal = dispatcher.seal(
                {**common, "type": "attempt-terminal", "status": "completed"},
                authority_kind="dispatcher",
            )
            rows.append(terminal)
            from loopzero.review.authority import review_terminal_acceptance_reasons

            assert not review_terminal_acceptance_reasons(terminal), (
                review_terminal_acceptance_reasons(terminal)
            )
            rows.append(
                seal(
                    {
                        "schema_version": projection.TELEMETRY_SCHEMA_VERSION,
                        "policy_version": common["policy_version"],
                        "type": "verdict",
                        "task_id": name,
                        "run_id": run,
                        "verdict": "pass",
                    }
                )
            )
            rows.append(
                seal(
                    review_state.settle_review_slot(
                        consumer,
                        rows,
                        reservation=admitted.slot,
                        outcome=ReviewOutcome.CONSUMED,
                        terminal_ref=terminal,
                    ).to_dict()
                )
            )
            return admitted

    return SimpleNamespace(
        repo=consumer,
        rows=rows,
        remote=remote,
        source=source,
        change=change,
        review=review,
        published=published,
    )


@pytest.mark.parametrize("outcome", ["repaired", "rejected", "unresolved"])
def test_native_primary_repair_delta_and_projection(delivery, outcome):
    d = delivery
    first = d.review("primary", d.change(1))
    primary_identity = d.source()
    assert (
        len(
            pr_review.blocking_findings(
                d.repo,
                d.rows,
                primary_task_id="primary",
                current_identity=primary_identity,
            )
        )
        == 1
    )
    d.review("delta", d.change(2), first=first, outcome=outcome)
    blockers = pr_review.blocking_findings(
        d.repo,
        d.rows,
        primary_task_id="primary",
        current_identity=d.source(),
        delta_task_id="delta",
    )
    assert bool(blockers) == (outcome == "unresolved")
    if blockers:
        return
    with pytest.raises(pr_review.DispatchError, match="projected"):
        pr_review.require_review_readiness(
            d.repo,
            d.rows,
            runner=d.remote,
            worktree=d.repo,
            primary_task_id="primary",
            current_identity=d.source(),
            delta_task_id="delta",
        )
    for name, identity in [("primary", primary_identity), ("delta", d.source())]:
        pr_review.ensure_projection(
            d.repo, d.rows, name, runner=d.remote, pr_identity=identity
        )
        pr_review.ensure_projection(
            d.repo, d.rows, name, runner=d.remote, pr_identity=identity
        )
    assert len(d.published) == 2
    pr_review.require_review_readiness(
        d.repo,
        d.rows,
        runner=d.remote,
        worktree=d.repo,
        primary_task_id="primary",
        current_identity=d.source(),
        delta_task_id="delta",
    )
    d.change(3)
    with pytest.raises(pr_review.DispatchError):
        pr_review.require_review_readiness(
            d.repo,
            d.rows,
            runner=d.remote,
            worktree=d.repo,
            primary_task_id="primary",
            current_identity=d.source(),
            delta_task_id="delta",
        )


def test_delta_new_material_findings_remain_blocking(delivery):
    d = delivery
    first = d.review("primary", d.change(1))
    d.review(
        "delta",
        d.change(2),
        first=first,
        new_findings=[
            {"severity": "critical", "claim": "New regression", "path": "owned.py"}
        ],
    )
    blockers = pr_review.blocking_findings(
        d.repo,
        d.rows,
        primary_task_id="primary",
        current_identity=d.source(),
        delta_task_id="delta",
    )
    assert [row["claim"] for row in blockers.values()] == ["New regression"]


def test_projection_uncertain_post_reconciles_and_edited_copy_is_not_reused(delivery):
    d = delivery
    d.review("primary", d.change(1))

    class Interrupted:
        def json(self, argv):
            value = d.remote.json(argv)
            if "POST" in argv:
                raise OSError("connection lost after remote success")
            return value

    pr_review.ensure_projection(
        d.repo, d.rows, "primary", runner=Interrupted(), pr_identity=d.source()
    )
    assert len(d.published) == 1
    d.published[0]["body"] = "edited"
    pr_review.ensure_projection(
        d.repo, d.rows, "primary", runner=Interrupted(), pr_identity=d.source()
    )
    assert len(d.published) == 2
    pr_review.ensure_projection(
        d.repo, d.rows, "primary", runner=Interrupted(), pr_identity=d.source()
    )
    assert len(d.published) == 2


def test_v2_capture_does_not_write_legacy_finding_state(delivery):
    d = delivery
    d.change(1)
    response = evidence.append_finding_records(
        d.repo,
        task_id="review",
        result={
            "findings": [
                {"severity": "important", "claim": "Must stay in signed result"}
            ]
        },
        snapshot=None,
        worktree=d.repo,
        advisory=False,
        source_head=d.source()["head"],
        review_intent="delivery-code-review",
        delivery_run_id=d.source()["run_id"],
        pr=7,
    )
    assert response is None
    assert not (d.repo / ".audit/findings").exists()


@pytest.mark.parametrize("dirty", ["tracked", "staged", "untracked"])
def test_dirty_source_cannot_admit_v2_delta(delivery, dirty):
    d = delivery
    first = d.review("primary", d.change(1))
    identity = d.change(2)
    path = d.repo / ("new.py" if dirty == "untracked" else "owned.py")
    path.write_text('VALUE = "uncommitted defect"\n')
    if dirty == "staged":
        git(d.repo, "add", "owned.py")
    with pytest.raises(AssertionError, match="Blocked"):
        d.review("delta", identity, first=first)


def test_delta_cannot_substitute_for_primary_with_unresolved_findings(delivery):
    d = delivery
    first = d.review("primary", d.change(1))
    d.review("delta", d.change(2), first=first, outcome="unresolved")
    pr_review.ensure_projection(
        d.repo, d.rows, "delta", runner=d.remote, pr_identity=d.source()
    )
    with pytest.raises(pr_review.DispatchError):
        pr_review.require_review_readiness(
            d.repo,
            d.rows,
            runner=d.remote,
            worktree=d.repo,
            primary_task_id="delta",
            current_identity=d.source(),
        )
