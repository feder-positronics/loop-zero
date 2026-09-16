"""Dirty reviews bind live source provenance to immutable snapshot content."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.kernel import authority as signing, authority_projection as projection
from loopzero.kernel import authority_store, worktree_lease
from loopzero.review import admission, chain, evidence
from conftest import git


@pytest.mark.parametrize("separate_worktree", [False, True])
@pytest.mark.parametrize(
    "tamper",
    [
        None,
        "tracked",
        "untracked",
        "identity",
        "parent",
        "source",
        "tree",
        "capture-drift",
    ],
)
def test_dirty_snapshot_admission(
    consumer,
    separate_worktree,
    tamper,
    monkeypatch,
    tmp_path,
    isolated_ptrace_scope_path,
):
    chain.configure(
        SimpleNamespace(
            required_sections=("code",), max_reviews_per_pr=1, max_delta_reviews=1
        )
    )
    monkeypatch.setattr(signing, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
    monkeypatch.setattr(
        signing, "_coordinator_state_directory", lambda: tmp_path / "signer"
    )
    coordinator = authority_store.create_coordinator_authority()
    rows = [
        coordinator.seal(
            {
                "type": "coordinator-authority-cutover",
                "status": "active",
                "ledger_prefix": projection.coordinator_ledger_prefix([]),
            },
            authority_kind="coordinator",
        )
    ]
    repo = consumer
    head = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-ref", "refs/remotes/origin/main", head)
    source = repo
    if separate_worktree:
        source = repo.parent / "linked"
        git(repo, "worktree", "add", "-b", "review", str(source))
    evidence.configure(
        SimpleNamespace(
            root=repo,
            audit_root=Path(".audit"),
            github=SimpleNamespace(ref_namespace="consumer-snapshots"),
        )
    )
    (source / "AGENTS.md").write_text("# Changed tracked content\n")
    (source / "new.py").write_text("VALUE = 42\n")
    expected = worktree_lease.source_identity(source)
    snapshot = evidence.create_review_snapshot(source, "dirty-review")
    identity = dict(snapshot.patch_identity)
    assert identity["candidate_sha"] != head
    if tamper == "tracked":
        (source / "AGENTS.md").write_text("# Drift\n")
    elif tamper == "untracked":
        (source / "new.py").write_text("VALUE = 43\n")
    elif tamper == "identity":
        identity["diff_sha256"] = "0" * 64
    elif tamper == "parent":
        from loopzero.kernel.patch_identity import compute_patch_identity

        head_tree = git(repo, "rev-parse", f"{head}^{{tree}}").strip()
        other = git(repo, "commit-tree", head_tree, "-m", "unrelated").strip()
        forged = git(
            repo, "commit-tree", snapshot.tree_sha, "-p", other, "-m", "forged"
        ).strip()
        identity = compute_patch_identity(repo, base_sha=other, candidate_sha=forged)
    elif tamper == "source":
        expected = {**expected, "state_sha256": "0" * 64}
    elif tamper == "tree":
        (source / "new.py").write_text("VALUE = 99\n")
        expected = worktree_lease.source_identity(source)
    elif tamper == "capture-drift":
        capture = evidence._capture_source_tree

        def drifting_capture(worktree, original_head):
            tree = capture(worktree, original_head)
            (worktree / "new.py").write_text("VALUE = 99\n")
            return tree

        monkeypatch.setattr(evidence, "_capture_source_tree", drifting_capture)
    with authority_store.authority_ledger_lock(repo):
        result = admission.admit_review(
            repo,
            rows,
            repository_binding=authority_store.authority_repository_binding(repo),
            task={
                "task_id": "dirty",
                "idempotency_key": "dirty",
                "review_intent": "delivery-code-review",
                "source_identity": expected,
            },
            current_source_identity=expected,
            current_tree_sha=snapshot.tree_sha,
            patch_identity=identity,
            required_sections=("code",),
            equivalence_proof=None,
            format_only_proof=None,
            requested="review",
            changed_paths=None,
            security_trigger_paths=(),
            source_worktree=source,
        )
    if tamper is None:
        assert isinstance(result, admission.Reserved), result
        assert result.scoped_task["source_identity"]["head"] == head
    else:
        assert isinstance(result, admission.Blocked), result
        assert result.code == "stale-source"
