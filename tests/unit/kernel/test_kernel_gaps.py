"""Behavior coverage for modules without a dedicated imported test file."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from loopzero.config import Profile
from loopzero.kernel import (authority_projection, authority_store, canonical,
                             git_config_security, gitscope, ledger_lifecycle,
                             run_identity, seams, trusted_exec, worktree_list,
                             worktree_prune)
from loopzero.kernel.settings import KernelSettings


def test_canonical_digest_is_order_independent_and_preserves_unicode():
    import hashlib
    expected = hashlib.sha256('{"a":"é","b":2}'.encode()).hexdigest()
    assert canonical.canonical_record_digest({"b": 2, "a": "é"}) == expected
    with pytest.raises(canonical.LedgerConflict):
        canonical.canonical_record_digest({"bad": object()})


def test_settings_from_profile_preserves_consumer_roots(tmp_path):
    profile = Profile(root=tmp_path, core_repository="example", core_revision="a" * 40,
                      core_path=Path("vendor/core"), profiles=(), checks={},
                      env_prefix="ACME", audit_root=Path("evidence"),
                      state_root="~/.local/state/acme", toolchain={"interpreter": "/usr/bin/python3"})
    settings = KernelSettings.from_profile(profile)
    assert settings.env("WORKTREE_LEASE_NONCE") == "ACME_WORKTREE_LEASE_NONCE"
    assert settings.temp_prefix == "acme-"
    assert settings.authority_lock_name == "acme-authority-ledger.lock"
    assert settings.account_state_root(tmp_path) == tmp_path / ".local/state/acme"
    assert settings.audit_root == Path("evidence")
    legacy = KernelSettings(env_prefix="INTELFLO")
    assert legacy.temp_prefix == "intelflo-"
    assert legacy.env("JOB_DIR") == "INTELFLO_JOB_DIR"


def test_named_review_seam_delegates_and_fails_closed(monkeypatch):
    monkeypatch.setattr(seams, "_adapters", {})
    with pytest.raises(seams.MissingAdapter, match="A4.*archived_supersession_deposits"):
        seams.archived_supersession_deposits([])
    seams.configure(archived_supersession_deposits=lambda rows: rows[:1])
    assert seams.archived_supersession_deposits([{"id": 1}, {"id": 2}]) == [{"id": 1}]
    with pytest.raises(ValueError):
        seams.configure(unknown=lambda: None)


def test_projection_rejects_boolean_attempt_indices():
    assert authority_projection._terminal_authority_attempt_key({"task_id": "one", "attempt_index": True}) is None
    assert authority_projection._terminal_authority_attempt_key({"task_id": "one", "attempt_index": 0}) == ("one", 0)


def test_authority_store_lock_is_reentrant_and_releases(tmp_path):
    (tmp_path / ".git").mkdir()
    with authority_store.authority_ledger_lock(tmp_path):
        assert str(tmp_path.resolve()) in authority_store._held_authority_ledger_locks()
        with authority_store.authority_ledger_lock(tmp_path):
            pass
    assert not authority_store._held_authority_ledger_locks()
    assert (tmp_path / ".git/intelflo-authority-ledger.lock").is_file()


def test_lifecycle_writes_complete_downgrade_barrier():
    checkpoint = {"created_at": "2026-09-11T00:00:00+00:00", "type": "checkpoint"}
    rows = [json.loads(row) for row in ledger_lifecycle._downgrade_barrier_payload(checkpoint, "f" * 32).splitlines()]
    assert rows[0] == checkpoint
    assert rows[1]["ownership_required"] is True
    assert rows[1]["run_id"] == "sr_" + "f" * 32
    assert rows[1]["status"] == "reserved"


def test_inventory_parses_registered_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(worktree_list.subprocess, "check_output", lambda *a, **k: f"worktree {tmp_path}\nHEAD {'a' * 40}\nbranch refs/heads/main\n\n")
    assert worktree_list.parse_worktrees(tmp_path) == [(tmp_path, "main")]
    assert worktree_list.tool_tag(tmp_path, tmp_path) == "primary"


def test_prune_only_removes_old_clean_owned_worktrees(monkeypatch, tmp_path, capsys):
    primary = tmp_path
    old = tmp_path / ".claude/worktrees/old"
    dirty = tmp_path / ".claude/worktrees/dirty"
    foreign = tmp_path / "other"
    monkeypatch.setattr(sys, "argv", ["worktree-prune", "--dry-run"])
    monkeypatch.setattr(worktree_prune, "repo_root", lambda: primary)
    monkeypatch.setattr(worktree_prune, "parse_worktrees", lambda _: [(p, "branch") for p in (primary, old, dirty, foreign)])
    monkeypatch.setattr(worktree_prune, "age_days", lambda _: 10)
    monkeypatch.setattr(worktree_prune, "dirty_count", lambda p: int(p == dirty))
    assert worktree_prune.main() == 0
    output = capsys.readouterr().out
    assert f"WOULD REMOVE {old}" in output
    assert f"WOULD REMOVE {foreign}" not in output
    assert f"SKIP {dirty}" in output


def test_trusted_executable_ignores_hostile_path(monkeypatch, tmp_path):
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nexit 42\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert trusted_exec.system_executable("git") != fake
    assert "LD_PRELOAD" not in trusted_exec.trusted_subprocess_environment({"LD_PRELOAD": "evil", "LANG": "C"})
    with pytest.raises(trusted_exec.TrustedExecutableError):
        trusted_exec.system_executable("../git")


def test_run_identity_extracts_exact_marker():
    run_id = "sr_" + "a" * 32
    assert run_identity.extract_run_id_marker(f"<!-- skill-run-id: {run_id} -->") == run_id
    assert run_identity.extract_run_id_marker("<!-- skill-run-id: forged -->") is None


def test_gitscope_hash_ignores_mutable_fields_and_enforces_scope():
    assert gitscope.scope_violations({"src/a.py", "secret"}, ["src/**"]) == ["secret"]
    assert gitscope.task_contract_hash({"id": "a", "paths": ["src/**"]}) != gitscope.task_contract_hash({"id": "b", "paths": ["src/**"]})


def test_git_configuration_digest_ignores_branch_metadata():
    base = b'[remote "origin"]\nurl = https://example.invalid/repo\n'
    assert git_config_security.security_projection_sha256(base) == git_config_security.security_projection_sha256(base + b'[branch "topic"]\nremote = origin\n')
