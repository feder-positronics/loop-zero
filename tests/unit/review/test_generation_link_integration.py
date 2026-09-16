"""Real signed-record reproduction for the missing normal delta link producer."""

import copy
import subprocess
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
from loopzero.review import admission, chain
from loopzero.runners.contract import ReviewOutcome


def git(root, *args):
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=probe",
            "-c",
            "user.email=probe@example.invalid",
            *args,
        ],
        text=True,
    )


def admit(repo, rows, identity, name, requested="review"):
    return admission.admit_review(
        repo,
        rows,
        repository_binding=authority_store._authority_repository_binding(repo),
        task={
            "task_id": name,
            "idempotency_key": name,
            "review_intent": "delivery-code-review",
            "run_id": "sr_" + "1" * 32,
        },
        current_source_identity=worktree_lease.source_identity(repo),
        current_tree_sha=identity["candidate_tree_sha"],
        patch_identity=identity,
        required_sections=("code",),
        equivalence_proof=None,
        format_only_proof=None,
        requested=requested,
        changed_paths=None,
        security_trigger_paths=(),
    )


@pytest.fixture
def signed_predecessor(consumer, monkeypatch, tmp_path, isolated_ptrace_scope_path):
    monkeypatch.setattr(signing, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
    monkeypatch.setattr(
        signing, "_coordinator_state_directory", lambda: tmp_path / "signer"
    )
    coordinator = authority_store.create_coordinator_authority()

    def seal(row):
        return coordinator.seal(row, authority_kind="coordinator")

    rows = [
        seal(
            {
                "type": "coordinator-authority-cutover",
                "status": "active",
                "ledger_prefix": projection.coordinator_ledger_prefix([]),
            }
        )
    ]
    base = git(consumer, "rev-parse", "HEAD").strip()
    (consumer / "owned.py").write_text("VALUE = 1\n")
    git(consumer, "add", "owned.py")
    git(consumer, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "first")
    first_head = git(consumer, "rev-parse", "HEAD").strip()
    from pathlib import Path

    from loopzero.review import evidence

    git(consumer, "update-ref", "refs/remotes/origin/main", base)
    evidence.configure(
        SimpleNamespace(
            root=consumer,
            audit_root=Path(".audit"),
            github=SimpleNamespace(ref_namespace="consumer-snapshots"),
        )
    )
    primary_snapshot = evidence.create_review_snapshot(consumer, "primary")
    assert primary_snapshot.commit_sha != first_head
    first_identity = patch_identity.capture_patch_identity(
        consumer, candidate_sha=first_head, base_ref=base
    )
    assert first_identity is not None
    with authority_store.authority_ledger_lock(consumer):
        chain.configure(
            SimpleNamespace(
                required_sections=("code",), max_reviews_per_pr=1, max_delta_reviews=1
            )
        )
        first = admit(consumer, rows, first_identity, "first-review")
        assert isinstance(first, admission.Reserved)
        rows.extend(seal(dict(row)) for row in first.records_to_append)
        dispatcher = signing.TerminalAuthority.generate()
        common = {
            "schema_version": projection.TELEMETRY_SCHEMA_VERSION,
            "policy_version": next(
                iter(projection.COMPATIBLE_DISPATCH_POLICY_VERSIONS)
            ),
            "task_id": "first-review",
            "work_unit_id": "first-review",
            "run_id": "sr_" + "1" * 32,
            "attempt_index": 0,
            "worktree": str(consumer),
            "read_only": True,
            "work_kind": "review",
            "review_intent": "delivery-code-review",
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
            {
                **common,
                "type": "attempt-terminal",
                "status": "completed",
                "snapshot_tree_sha": first.generation.tree,
                "patch_identity": first_identity,
                "source_identity": worktree_lease.source_identity(consumer),
                "snapshot_sha": primary_snapshot.commit_sha,
                "review_generation_id": first.generation.generation_id,
                "review_reservation_id": first.slot.reservation_id,
            },
            authority_kind="dispatcher",
        )
        rows.append(terminal)
        rows.append(
            seal(
                {
                    "schema_version": projection.TELEMETRY_SCHEMA_VERSION,
                    "policy_version": next(
                        iter(projection.COMPATIBLE_DISPATCH_POLICY_VERSIONS)
                    ),
                    "type": "verdict",
                    "task_id": "first-review",
                    "run_id": common["run_id"],
                    "verdict": "pass",
                }
            )
        )
        assert id(terminal) in projection._authenticated_attempt_terminal_ids(rows)
        settlement = review_state.settle_review_slot(
            consumer,
            rows,
            reservation=first.slot,
            outcome=ReviewOutcome.CONSUMED,
            terminal_ref=terminal,
        )
        rows.append(seal(settlement.to_dict()))
        state = projection.slot_state(rows, first.generation.generation_id, "delivery")
        assert state.primary_consumed
        assert not state.delta_consumed
    (consumer / "owned.py").write_text("VALUE = 2\n")
    git(consumer, "add", "owned.py")
    git(consumer, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "repair")
    target = patch_identity.capture_patch_identity(
        consumer,
        candidate_sha=git(consumer, "rev-parse", "HEAD").strip(),
        base_ref=base,
    )
    assert target is not None
    return consumer, rows, first, target, seal


def test_normal_changed_source_delta_inherits_authenticated_predecessor(
    signed_predecessor,
):
    repo, rows, first, target, seal = signed_predecessor
    result = linked(repo, PersistentRecords(repo, rows, seal), target)
    assert isinstance(result, admission.Reserved), result
    assert result.generation.predecessor_id == first.generation.generation_id
    assert result.generation.lineage_id == first.generation.lineage_id
    assert result.slot.slot_kind == "delta"


@pytest.mark.parametrize("field", ("run_id", "source_identity", "review_generation_id"))
def test_changed_terminal_claims_do_not_authenticate(signed_predecessor, field):
    _repo, rows, _first, _target, _seal = signed_predecessor
    original = next(row for row in rows if row.get("type") == "attempt-terminal")
    damaged = copy.deepcopy(original)
    damaged[field] = "forged"
    altered = [damaged if row is original else row for row in rows]
    assert id(damaged) not in projection._authenticated_attempt_terminal_ids(altered)


def test_unsigned_generation_link_cannot_supply_missing_authority(signed_predecessor):
    repo, rows, first, target, _seal = signed_predecessor
    terminal = next(row for row in rows if row.get("type") == "attempt-terminal")
    link = review_state.GenerationLinkV1(
        repository_binding=authority_store._authority_repository_binding(repo),
        predecessor_generation_id=first.generation.generation_id,
        from_identity=terminal["patch_identity"],
        to_identity=target,
        transition_kind="substantive",
    )
    with authority_store.authority_ledger_lock(repo):
        result = admit(repo, [*rows, link.to_dict()], target, "delta-review", "delta")
    assert isinstance(result, admission.Blocked)
    assert result.code == "missing-evidence"


def add_link(rows, first, target, seal):
    rows.append(
        seal(
            review_state.GenerationLinkV1(
                repository_binding=first.generation.repository_binding,
                predecessor_generation_id=first.generation.generation_id,
                from_identity=first.generation.patch_identity,
                to_identity=target,
                transition_kind="substantive",
            ).to_dict()
        )
    )


def changed(repo, target):
    (repo / "owned.py").write_text("VALUE = 3\n")
    git(repo, "add", "owned.py")
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "second repair")
    return patch_identity.capture_patch_identity(
        repo,
        candidate_sha=git(repo, "rev-parse", "HEAD").strip(),
        base_ref=target["base_sha"],
    )


def consume(repo, rows, reserved, seal):
    dispatcher = signing.TerminalAuthority.generate()
    common = {
        "schema_version": projection.TELEMETRY_SCHEMA_VERSION,
        "policy_version": next(iter(projection.COMPATIBLE_DISPATCH_POLICY_VERSIONS)),
        "task_id": reserved.slot.task_id,
        "work_unit_id": reserved.slot.task_id,
        "run_id": "sr_" + "1" * 32,
        "attempt_index": 0,
        "worktree": str(repo),
        "read_only": True,
        "work_kind": "review",
        "review_intent": "delivery-code-review",
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
        {
            **common,
            "type": "attempt-terminal",
            "status": "completed",
            "snapshot_sha": reserved.generation.patch_identity["candidate_sha"],
            "snapshot_tree_sha": reserved.generation.tree,
            "patch_identity": reserved.generation.patch_identity,
            "source_identity": worktree_lease.source_identity(repo),
            "review_generation_id": reserved.generation.generation_id,
            "review_reservation_id": reserved.slot.reservation_id,
        },
        authority_kind="dispatcher",
    )
    rows.append(terminal)
    rows.append(seal({**common, "type": "verdict", "verdict": "pass"}))
    rows.append(
        seal(
            review_state.settle_review_slot(
                repo,
                rows,
                reservation=reserved.slot,
                outcome=ReviewOutcome.CONSUMED,
                terminal_ref=terminal,
            ).to_dict()
        )
    )
    assert projection.slot_state(
        rows, reserved.generation.generation_id, "delivery"
    ).delta_consumed


@pytest.mark.parametrize("settled", (False, True))
@pytest.mark.parametrize("older", (False, True))
def test_lineage_cannot_fund_second_delta(signed_predecessor, settled, older):
    repo, rows, first, target, seal = signed_predecessor
    with authority_store.authority_ledger_lock(repo):
        add_link(rows, first, target, seal)
        delta = admit(repo, rows, target, "first-delta", "delta")
        assert isinstance(delta, admission.Reserved)
        rows.extend(seal(dict(row)) for row in delta.records_to_append)
        if settled:
            consume(repo, rows, delta, seal)
        successor = changed(repo, target)
    store = PersistentRecords(repo, rows, seal)
    locator = next(
        (
            row["snapshot_sha"]
            for row in rows
            if row.get("type") == "attempt-terminal"
            and row["task_id"] == (first if older else delta).slot.task_id
        ),
        target["candidate_sha"],
    )
    before = len(store.load())
    result = linked(repo, store, successor, delta_from_snapshot=locator)
    assert isinstance(result, admission.Blocked), result
    assert len(store.load()) == before


class PersistentRecords:
    def __init__(self, repo, rows, seal):
        import json

        self.path = repo / ".git" / "signed-test-records.jsonl"
        self.seal = seal
        self.fail_after = None
        self.count = 0
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def load(self):
        import json

        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def append(self, records):
        import json
        import os

        for record in records:
            with self.path.open("a") as stream:
                stream.write(json.dumps(self.seal(dict(record))) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.count += 1
            if self.count == self.fail_after:
                raise InterruptedError("synthetic durable append interruption")


def linked(repo, store, target, **overrides):
    from loopzero.kernel.gitscope import ReviewSnapshot
    from loopzero.review.lineage import admit_linked_delta

    task = {
        "task_id": "linked-delta",
        "idempotency_key": "linked-delta",
        "run_id": "sr_" + "1" * 32,
        "review_intent": "delivery-code-review",
        "delta_from_snapshot": next(
            row["snapshot_sha"]
            for row in store.load()
            if row.get("type") == "attempt-terminal"
        ),
    }
    task.update(overrides)
    return admit_linked_delta(
        repo,
        worktree=repo,
        task=task,
        expected_source=worktree_lease.source_identity(repo),
        snapshot=ReviewSnapshot(
            target["candidate_sha"], target["candidate_tree_sha"], repo, target
        ),
        load_records=store.load,
        append_records=store.append,
        required_sections=("code",),
    )


def test_persistent_link_admission_and_duplicate_retry(signed_predecessor):
    repo, rows, first, target, seal = signed_predecessor
    store = PersistentRecords(repo, rows, seal)
    result = linked(repo, store, target)
    assert isinstance(result, admission.Reserved)
    assert result.generation.lineage_id == first.generation.lineage_id
    retry = linked(repo, store, target)
    assert not retry.dispatch
    assert len(projection.generation_links(store.load())) == 1


@pytest.mark.parametrize("boundary", (1, 2, 3, 4))
def test_persistent_interruption_never_renews_launch(signed_predecessor, boundary):
    repo, rows, _first, target, seal = signed_predecessor
    store = PersistentRecords(repo, rows, seal)
    store.fail_after = boundary
    with pytest.raises(InterruptedError):
        linked(repo, store, target)
    store.fail_after = None
    retry = linked(repo, store, target)
    assert retry.dispatch == (boundary < 4)
    # A persisted reservation is spent launch permission, even if caller saw no result.
    reservations = [
        row
        for row in store.load()
        if row.get("type") == "review-slot-reservation-v1"
        and row.get("slot_kind") == "delta"
    ]
    assert len(reservations) == 1
    assert not linked(repo, store, target).dispatch
    assert len(projection.generation_links(store.load())) == 1


@pytest.mark.parametrize(
    "overrides",
    (
        {"run_id": "sr_" + "2" * 32},
        {"review_intent": "trust-manifest-verification"},
        {"delta_from_snapshot": "f" * 40},
        {"review_generation_id": "cg_" + "f" * 32},
    ),
)
def test_foreign_launch_cannot_reuse_content_edge(signed_predecessor, overrides):
    repo, rows, _first, target, seal = signed_predecessor
    store = PersistentRecords(repo, rows, seal)
    store.fail_after = 1
    with pytest.raises(InterruptedError):
        linked(repo, store, target)
    store.fail_after = None
    count = len(store.load())
    assert not linked(repo, store, target, **overrides).dispatch
    assert len(store.load()) == count
    assert isinstance(linked(repo, store, target), admission.Reserved)


@pytest.mark.parametrize("when", ("load", "link", "admission"))
def test_source_mutation_never_launches(signed_predecessor, when):
    repo, rows, _first, target, seal = signed_predecessor
    store = PersistentRecords(repo, rows, seal)
    original_load, original_append = store.load, store.append
    reads = 0

    def load():
        nonlocal reads
        reads += 1
        # linked() first reads only to locate the predecessor, before capture.
        if when == "load" and reads == 2:
            (repo / "owned.py").write_text("MUTATED = True\n")
        return original_load()

    def append(records):
        original_append(records)
        if (
            when == "link" and records[0].get("type") == "review-generation-link-v1"
        ) or (
            when == "admission"
            and records[0].get("type") != "review-generation-link-v1"
        ):
            (repo / "owned.py").write_text("MUTATED = True\n")

    store.load, store.append = load, append
    assert not linked(repo, store, target).dispatch


@pytest.mark.parametrize(
    "kind", ("unsigned", "duplicate", "wrong-predecessor", "wrong-source")
)
def test_invalid_persisted_edge_never_launches(signed_predecessor, kind):
    import json

    repo, rows, first, target, seal = signed_predecessor
    store = PersistentRecords(repo, rows, seal)
    link = review_state.GenerationLinkV1(
        first.generation.repository_binding,
        first.generation.generation_id,
        first.generation.patch_identity,
        target,
        "substantive",
    ).to_dict()
    if kind == "wrong-predecessor":
        link["predecessor_generation_id"] = "cg_" + "f" * 32
    if kind == "wrong-source":
        link["from_identity"] = target
    if kind == "unsigned":
        # Simulate a storage adapter that returns without authenticating a write.
        def append(records):
            with store.path.open("a") as stream:
                for row in records:
                    stream.write(json.dumps(dict(row)) + "\n")

        store.append = append
    else:
        store.append((link, link) if kind == "duplicate" else (link,))
    assert not linked(repo, store, target).dispatch


def test_link_does_not_renew_primary_across_run(signed_predecessor):
    repo, rows, first, target, seal = signed_predecessor
    with authority_store.authority_ledger_lock(repo):
        add_link(rows, first, target, seal)
        result = admit(repo, rows, target, "new-primary", "review")
    assert isinstance(result, admission.Blocked)


def test_compacted_consumed_delta_still_blocks_older_predecessor(signed_predecessor):
    import json

    from loopzero.kernel import ledger_lifecycle
    from loopzero.kernel.policy import DISPATCH_DIR

    repo, rows, first, target, seal = signed_predecessor
    with authority_store.authority_ledger_lock(repo):
        add_link(rows, first, target, seal)
        delta = admit(repo, rows, target, "first-delta", "delta")
        rows.extend(seal(dict(row)) for row in delta.records_to_append)
        consume(repo, rows, delta, seal)
    directory = repo / DISPATCH_DIR
    directory.mkdir(parents=True)
    (directory / "2026-09-16.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    (repo / ".git" / "info" / "exclude").write_text(".audit/\n")
    ledger_lifecycle.compact_authority_ledger(repo)
    compacted = authority_store.load_authority_snapshot(repo).records
    assert isinstance(compacted, projection.AuthorityRecordView)
    assert projection.slot_state(
        compacted, delta.generation.generation_id, "delivery"
    ).delta_consumed
    successor = changed(repo, target)
    # Producer must reject from a real protected, compacted view, without writes.
    store = PersistentRecords(repo, [], seal)
    store.load = lambda: compacted
    result = linked(repo, store, successor)
    assert not result.dispatch
    assert store.count == 0


@pytest.mark.parametrize("separate_worktree", (False, True))
def test_real_snapshot_commit_is_not_source_head(signed_predecessor, separate_worktree):
    from pathlib import Path

    from loopzero.review import evidence
    from loopzero.review.lineage import admit_linked_delta

    repo, rows, _first, target, seal = signed_predecessor
    git(repo, "update-ref", "refs/remotes/origin/main", target["base_sha"])
    evidence.configure(
        SimpleNamespace(
            root=repo,
            audit_root=Path(".audit"),
            github=SimpleNamespace(ref_namespace="consumer-snapshots"),
        )
    )
    worktree = repo
    if separate_worktree:
        worktree = repo.parent / "task-worktree"
        git(repo, "checkout", "--detach", "-q")
        git(repo, "worktree", "add", str(worktree), "main")
    snapshot = evidence.create_review_snapshot(worktree, "real-snapshot")
    assert snapshot.commit_sha != target["candidate_sha"]
    assert snapshot.patch_identity == target
    store = PersistentRecords(repo, rows, seal)
    terminal = next(row for row in rows if row.get("type") == "attempt-terminal")
    result = admit_linked_delta(
        repo,
        worktree=worktree,
        task={
            "task_id": "real-delta",
            "idempotency_key": "real-delta",
            "run_id": terminal["run_id"],
            "review_intent": "delivery-code-review",
            "delta_from_snapshot": terminal["snapshot_sha"],
        },
        expected_source=worktree_lease.source_identity(worktree),
        snapshot=snapshot,
        load_records=store.load,
        append_records=store.append,
        required_sections=("code",),
    )
    assert isinstance(result, admission.Reserved), result


@pytest.mark.parametrize(
    "omitted", ("review-generation-v1", "review-family-coverage-v1")
)
def test_incomplete_persistence_cannot_authorize_launch(signed_predecessor, omitted):
    repo, rows, _first, target, seal = signed_predecessor
    store = PersistentRecords(repo, rows, seal)
    append = store.append

    def incomplete(records):
        append(tuple(row for row in records if row.get("type") != omitted))

    store.append = incomplete
    assert not linked(repo, store, target).dispatch


def test_final_reload_mutation_cannot_authorize_launch(signed_predecessor):
    repo, rows, _first, target, seal = signed_predecessor
    store = PersistentRecords(repo, rows, seal)
    load = store.load

    def mutating_load():
        result = load()
        if any(
            row.get("type") == "review-slot-reservation-v1"
            and row.get("slot_kind") == "delta"
            for row in result
        ):
            (repo / "owned.py").write_text("FINAL_MUTATION = True\n")
        return result

    store.load = mutating_load
    assert not linked(repo, store, target).dispatch


@pytest.mark.parametrize(
    "overrides",
    ({"run_id": "sr_" + "2" * 32}, {"review_intent": "trust-manifest-verification"}),
)
def test_compacted_edge_does_not_supply_foreign_launch_authority(
    signed_predecessor, overrides
):
    import json

    from loopzero.kernel import ledger_lifecycle
    from loopzero.kernel.gitscope import ReviewSnapshot
    from loopzero.kernel.policy import DISPATCH_DIR
    from loopzero.review.lineage import LinkRefused, _derive

    repo, rows, first, target, seal = signed_predecessor
    add_link(rows, first, target, seal)
    directory = repo / DISPATCH_DIR
    directory.mkdir(parents=True)
    (directory / "2026-09-16.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    (repo / ".git" / "info" / "exclude").write_text(".audit/\n")
    ledger_lifecycle.compact_authority_ledger(repo)
    records = authority_store.load_authority_snapshot(repo).records
    terminal = next(row for row in records if row.get("type") == "attempt-terminal")
    task = {
        "task_id": "compact-retry",
        "idempotency_key": "compact-retry",
        "run_id": terminal["run_id"],
        "review_intent": "delivery-code-review",
        "delta_from_snapshot": terminal["snapshot_sha"],
    }
    snapshot = ReviewSnapshot(
        target["candidate_sha"], target["candidate_tree_sha"], repo, target
    )
    source = worktree_lease.source_identity(repo)
    with (
        worktree_lease.worktree_lease(repo, boundary="test-link"),
        authority_store.authority_ledger_lock(repo),
    ):
        _link, existing = _derive(repo, repo, records, task, source, snapshot)
        assert existing
        with pytest.raises(LinkRefused):
            _derive(repo, repo, records, {**task, **overrides}, source, snapshot)


def test_independent_clone_cannot_borrow_repository_authority(signed_predecessor):
    from loopzero.kernel.gitscope import ReviewSnapshot
    from loopzero.review.lineage import admit_linked_delta

    repo, rows, _first, target, seal = signed_predecessor
    foreign = repo.parent / "foreign-clone"
    subprocess.run(
        ["git", "clone", "--no-local", str(repo), str(foreign)],
        check=True,
        capture_output=True,
    )
    terminal = next(row for row in rows if row.get("type") == "attempt-terminal")
    git(foreign, "fetch", str(repo), terminal["snapshot_sha"])
    store = PersistentRecords(repo, rows, seal)
    result = admit_linked_delta(
        repo,
        worktree=foreign,
        task={
            "task_id": "foreign",
            "idempotency_key": "foreign",
            "run_id": terminal["run_id"],
            "review_intent": "delivery-code-review",
            "delta_from_snapshot": terminal["snapshot_sha"],
        },
        expected_source=worktree_lease.source_identity(foreign),
        snapshot=ReviewSnapshot(
            target["candidate_sha"], target["candidate_tree_sha"], foreign, target
        ),
        load_records=store.load,
        append_records=store.append,
        required_sections=("code",),
    )
    assert isinstance(result, admission.Blocked), result
    assert store.count == 0


def test_caller_cannot_narrow_primary_sections(signed_predecessor):
    from loopzero.kernel.gitscope import ReviewSnapshot
    from loopzero.review.lineage import admit_linked_delta

    repo, rows, _first, target, seal = signed_predecessor
    terminal = next(row for row in rows if row.get("type") == "attempt-terminal")
    store = PersistentRecords(repo, rows, seal)
    result = admit_linked_delta(
        repo,
        worktree=repo,
        task={
            "task_id": "narrowed",
            "idempotency_key": "narrowed",
            "run_id": terminal["run_id"],
            "review_intent": "delivery-code-review",
            "delta_from_snapshot": terminal["snapshot_sha"],
        },
        expected_source=worktree_lease.source_identity(repo),
        snapshot=ReviewSnapshot(
            target["candidate_sha"], target["candidate_tree_sha"], repo, target
        ),
        load_records=store.load,
        append_records=store.append,
        required_sections=("security",),
    )
    assert isinstance(result, admission.Blocked), result
    assert store.count == 0
