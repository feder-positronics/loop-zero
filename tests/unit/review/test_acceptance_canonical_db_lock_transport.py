"""Regression coverage for canonical host DB-lock transport normalization."""

import fcntl
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


from loopzero.review import acceptance
from loopzero.kernel.gitscope import ReviewSnapshot, task_contract_hash


INNER = "uv run pytest " "tests/integration/api/test_inquiry_unified_evidence_api.py -q"


def _canonical_lock(primary: Path) -> Path:
    return primary / ".audit" / "dispatch" / "test-db.lock"


def _command(primary: Path, *, executable: str = "flock", cd: bool = False) -> str:
    prefix = "cd fastapi_backend && " if cd else ""
    return f"{prefix}{executable} -x {_canonical_lock(primary)} {INNER}"


def _patch_primary(monkeypatch: pytest.MonkeyPatch, primary: Path) -> None:
    profile = SimpleNamespace(
        toolchain={"db_lock": str(_canonical_lock(primary)), "dotenv": "fastapi_backend/.env"},
        audit_root=Path(".audit"), env_prefix="INTELFLO",
    )
    monkeypatch.setattr(acceptance, "_profile", lambda: profile)


@pytest.mark.parametrize("executable", ["flock", "/usr/bin/flock"])
@pytest.mark.parametrize("cd", [False, True])
def test_canonical_wrapper_grants_only_strict_inner_database_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, executable: str, cd: bool
) -> None:
    primary = tmp_path / "primary"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _patch_primary(monkeypatch, primary)
    original = _command(primary, executable=executable, cd=cd)
    expected_transport = ("cd fastapi_backend && " if cd else "") + INNER
    assert acceptance._acceptance_network_grant(original, explicit_db=False) is False
    assert (
        acceptance._acceptance_network_grant(expected_transport, explicit_db=False)
        is True
    )
    observed: list[tuple[str, bool | None]] = []

    def run_one(
        command: str,
        *,
        worktree: Path,
        timeout_s: int,
        db_bound: bool,
        db_network_grant: bool | None,
        authority: object,
    ) -> dict[str, object]:
        del worktree, timeout_s, authority
        assert db_bound is True
        observed.append((command, db_network_grant))
        contender = _canonical_lock(primary).open("a")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            contender.close()
        return {"command": command, "exit_code": 0, "tail": "one passed"}

    monkeypatch.setattr(acceptance, "_run_one_acceptance_command", run_one)

    result = acceptance.run_acceptance_commands(
        [original], worktree=worktree, timeout_s=1, authority=object()
    )[0]

    assert observed == [(expected_transport, True)]
    assert result == {
        "command": original,
        "transport_command": expected_transport,
        "exit_code": 0,
        "tail": "one passed",
        "db_bound": True,
    }
    with _canonical_lock(primary).open("a") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_transport_preserves_inner_bytes_and_explicit_db_network_grant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary = tmp_path / "primary"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _patch_primary(monkeypatch, primary)
    inner = (
        "uv  run pytest  "
        "tests/integration/api/test_inquiry_unified_evidence_api.py  -q"
    )
    original = f"flock -x {_canonical_lock(primary)} {inner}"
    observed: list[tuple[str, bool | None]] = []

    def run_one(command: str, **kwargs: object) -> dict[str, object]:
        observed.append((command, kwargs["db_network_grant"]))
        return {"command": command, "exit_code": 0, "tail": ""}

    monkeypatch.setattr(acceptance, "_run_one_acceptance_command", run_one)

    result = acceptance.run_acceptance_commands(
        [],
        db_commands=[original],
        worktree=worktree,
        timeout_s=1,
        authority=object(),
    )[0]

    assert observed == [(inner, True)]
    assert result["command"] == original
    assert result["transport_command"] == inner


def test_transport_is_not_derived_without_a_held_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary = tmp_path / "primary"
    original = _command(primary)
    _patch_primary(monkeypatch, primary)

    assert (
        acceptance._canonical_db_lock_transport_command(
            original, worktree=tmp_path, db_lock=None
        )
        is None
    )


def test_lock_timeout_preserves_original_without_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary = tmp_path / "primary"
    original = _command(primary)
    _patch_primary(monkeypatch, primary)
    monkeypatch.setattr(
        acceptance, "_acquire_test_db_lock", lambda worktree, timeout_s: None
    )
    monkeypatch.setattr(
        acceptance,
        "_run_one_acceptance_command",
        lambda *args, **kwargs: pytest.fail("timed-out lock must not execute"),
    )

    result = acceptance.run_acceptance_commands(
        [original], worktree=tmp_path, timeout_s=1, authority=object()
    )[0]

    assert result["command"] == original
    assert result["exit_code"] == 124
    assert "transport_command" not in result


@pytest.mark.parametrize(
    "template",
    [
        "flock -x {other} {inner}",
        "flock -s {lock} {inner}",
        "flock {lock} {inner}",
        "flock -x -w 1 {lock} {inner}",
        "flock -x {lock} -c '{inner}'",
        "flock -x 9 {inner}",
        "flock -x {lock} 9 {inner}",
        "flock -x {lock} sh -c '{inner}'",
        "flock -x {lock} {inner} && echo unsafe",
        "flock -x {lock} {inner} $(echo unsafe)",
        "flock -x {lock} {inner} `echo unsafe`",
        "flock -x {lock} {inner} > result.txt",
        "flock -x {lock} echo tests/integration/api/test_example.py",
        "flock -x {lock} uv run pytest tests/unit/test_example.py -q",
        "flock -x {lock} {inner} $UNTRUSTED",
    ],
)
def test_noncanonical_or_unsafe_wrappers_are_not_normalized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    template: str,
) -> None:
    primary = tmp_path / "primary"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _patch_primary(monkeypatch, primary)
    original = template.format(
        lock=_canonical_lock(primary), other=tmp_path / "other.lock", inner=INNER
    )
    observed: list[tuple[str, bool | None]] = []

    def run_one(command: str, **kwargs: object) -> dict[str, object]:
        observed.append((command, kwargs["db_network_grant"]))
        return {"command": command, "exit_code": 0, "tail": ""}

    monkeypatch.setattr(acceptance, "_run_one_acceptance_command", run_one)

    result = acceptance.run_acceptance_commands(
        [original], worktree=worktree, timeout_s=1, authority=object()
    )[0]

    assert observed == [(original, False)]
    assert result["command"] == original
    assert "transport_command" not in result


def test_transport_is_hash_bound_to_original_and_validated_in_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary = tmp_path / "primary"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _patch_primary(monkeypatch, primary)
    original = _command(primary)

    def run_one(command: str, **_kwargs: object) -> dict[str, object]:
        return {"command": command, "exit_code": 0, "tail": "one passed"}

    monkeypatch.setattr(acceptance, "_run_one_acceptance_command", run_one)
    results = acceptance.run_acceptance_commands(
        [original], worktree=worktree, timeout_s=1, authority=object()
    )
    task = {
        "task_id": "canonical-lock-review",
        "work_unit_id": "canonical-lock-review",
        "objective": "review canonical lock transport",
        "work_kind": "review",
        "acceptance_commands": [original],
    }
    snapshot = ReviewSnapshot(
        commit_sha="a" * 40, tree_sha="b" * 40, directory=tmp_path / "snapshot"
    )
    source = {"head": "c" * 40}
    receipt = acceptance.build_review_acceptance_receipt(
        task=task,
        source_identity=source,
        snapshot=snapshot,
        worktree=worktree,
        results=results,
        provenance="pre-model",
    )

    expected_digest = hashlib.sha256(
        json.dumps([original], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert receipt["declared_commands"] == [original]
    assert receipt["declared_commands_sha256"] == expected_digest
    assert receipt["results"] == [
        {
            "declared_command": original,
            "executed_command": original,
            "transport_command": INNER,
            "exit_code": 0,
            "tail": "one passed",
            "db_bound": True,
        }
    ]
    record = {
        "task_contract": task,
        "task_contract_hash": task_contract_hash(task),
        "source_identity": source,
        "snapshot_sha": snapshot.commit_sha,
        "snapshot_tree_sha": snapshot.tree_sha,
        "worktree": str(worktree),
        "acceptance": results,
    }
    assert acceptance.review_acceptance_receipt_reasons(record, receipt) == ()

    receipt["results"][0]["transport_command"] = "true"
    assert "review-acceptance-result-mismatch" in (
        acceptance.review_acceptance_receipt_reasons(record, receipt)
    )


def test_foreign_lock_handle_cannot_authorize_canonical_wrapper(monkeypatch, tmp_path):
    primary = tmp_path / "primary"
    _patch_primary(monkeypatch, primary)
    lock = _canonical_lock(primary)
    lock.parent.mkdir(parents=True)
    lock.touch()
    original = _command(primary)
    observed = []

    def run_one(command, **kwargs):
        observed.append((command, kwargs["db_network_grant"]))
        return {"command": command, "exit_code": 0, "tail": ""}

    monkeypatch.setattr(acceptance, "_run_one_acceptance_command", run_one)
    monkeypatch.setattr(
        acceptance,
        "_acquire_test_db_lock",
        lambda worktree, timeout_s: (tmp_path / "foreign.lock").open("a"),
    )
    result = acceptance.run_acceptance_commands(
        [original], worktree=tmp_path, timeout_s=1, authority=object()
    )[0]
    assert observed == [(original, False)]
    assert "transport_command" not in result
