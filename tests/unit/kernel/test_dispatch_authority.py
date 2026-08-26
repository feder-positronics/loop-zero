import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def load_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    scripts_dir = repo_root / "scripts" / "util"
    sys.path.insert(0, str(scripts_dir))
    module_path = scripts_dir / "dispatch_authority.py"
    spec = importlib.util.spec_from_file_location("dispatch_authority", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


@pytest.fixture(autouse=True)
def isolated_process_memory_contract(
    monkeypatch: pytest.MonkeyPatch,
    isolated_ptrace_scope_path: Path,
    tmp_path: Path,
) -> None:
    """Keep unit tests independent of the runner's Yama configuration."""
    monkeypatch.setattr(module, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path)
    monkeypatch.setattr(
        module,
        "_coordinator_state_directory",
        lambda: tmp_path / "dispatch-authority",
        raising=False,
    )


def terminal_record() -> dict[str, object]:
    return {
        "type": "attempt-terminal",
        "task_id": "dispatch-authenticated",
        "work_unit_id": "phase-1",
        "attempt_index": 0,
        "run_id": "sr_" + "1" * 32,
        "worktree": "/tmp/worktree",
        "status": "completed",
        "output_identity": {"tree_sha": "a" * 40},
        "ts": "2026-08-25T00:00:00+00:00",
        "schema_version": "dispatch-telemetry-v9",
        "policy_version": "2026-08-25-v12",
    }


def test_ed25519_authority_round_trip_binds_exact_record() -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer.seal(terminal_record(), authority_kind="dispatcher")

    module.verify_terminal_authority(
        sealed,
        registration=signer.registration(),
        expected_kind="dispatcher",
    )


def test_authority_requires_kernel_process_memory_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ptrace_scope = tmp_path / "ptrace_scope"
    ptrace_scope.write_text("0\n", encoding="ascii")
    monkeypatch.setattr(module, "PTRACE_SCOPE_PATH", ptrace_scope)

    with pytest.raises(module.TerminalAuthorityError, match="process memory isolation"):
        module.TerminalAuthority.generate()


def test_authority_kind_is_bound_inside_v2_signature() -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer.seal(terminal_record(), authority_kind="dispatcher")
    proof = sealed["terminal_authority_proof"]
    proof["authority_kind"] = "coordinator"
    proof["public_key"] = module.base64.b64encode(signer.public_key).decode("ascii")

    with pytest.raises(module.TerminalAuthorityError, match="signature"):
        module.verify_terminal_authority(
            sealed,
            registration=None,
            expected_kind="coordinator",
        )


def test_v1_authority_proof_remains_verifiable() -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer._seal(
        terminal_record(),
        authority_kind="dispatcher",
        scheme=module.LEGACY_AUTHORITY_SCHEME,
    )
    registration = signer.registration()
    registration["scheme"] = module.LEGACY_AUTHORITY_SCHEME

    module.verify_terminal_authority(
        sealed,
        registration=registration,
        expected_kind="dispatcher",
    )


def test_v1_dispatcher_proof_cannot_be_relabelled_as_coordinator() -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer._seal(
        terminal_record(),
        authority_kind="dispatcher",
        scheme=module.LEGACY_AUTHORITY_SCHEME,
    )
    proof = sealed["terminal_authority_proof"]
    proof["authority_kind"] = "coordinator"
    proof["public_key"] = module.base64.b64encode(signer.public_key).decode("ascii")

    with pytest.raises(module.TerminalAuthorityError, match="legacy"):
        module.verify_terminal_authority(
            sealed,
            registration=None,
            expected_kind="coordinator",
        )


@pytest.mark.parametrize("scheme", [[], {}])
def test_unhashable_scheme_fails_closed(scheme: object) -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer.seal(terminal_record(), authority_kind="dispatcher")
    sealed["terminal_authority_proof"]["scheme"] = scheme

    with pytest.raises(module.TerminalAuthorityError, match="scheme"):
        module.verify_terminal_authority(
            sealed,
            registration=signer.registration(),
            expected_kind="dispatcher",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "blocked"),
        ("run_id", "sr_" + "2" * 32),
        ("worktree", "/tmp/other"),
        ("output_identity", {"tree_sha": "b" * 40}),
    ],
)
def test_authority_rejects_terminal_mutation(field: str, value: object) -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer.seal(terminal_record(), authority_kind="dispatcher")
    sealed[field] = value

    with pytest.raises(module.TerminalAuthorityError, match="signature"):
        module.verify_terminal_authority(
            sealed,
            registration=signer.registration(),
            expected_kind="dispatcher",
        )


def test_authority_rejects_wrong_prelaunch_key() -> None:
    signer = module.TerminalAuthority.generate()
    other = module.TerminalAuthority.generate()
    sealed = signer.seal(terminal_record(), authority_kind="dispatcher")

    with pytest.raises(module.TerminalAuthorityError, match="key"):
        module.verify_terminal_authority(
            sealed,
            registration=other.registration(),
            expected_kind="dispatcher",
        )


def test_invalid_signature_verification_is_cached(monkeypatch) -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer.seal(terminal_record(), authority_kind="dispatcher")
    sealed["terminal_authority_proof"]["signature"] = module.base64.b64encode(
        b"\0" * 64
    ).decode("ascii")
    module._signature_is_valid.cache_clear()
    original_run = module.subprocess.run
    calls = 0

    def counted_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_run(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", counted_run)
    for _ in range(2):
        with pytest.raises(module.TerminalAuthorityError, match="signature"):
            module.verify_terminal_authority(
                sealed,
                registration=signer.registration(),
                expected_kind="dispatcher",
            )

    assert calls == 1


def test_coordinator_proof_carries_only_public_material() -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer.seal(
        terminal_record(), authority_kind="coordinator", include_public_key=True
    )

    module.verify_terminal_authority(
        sealed,
        registration=None,
        expected_kind="coordinator",
    )
    rendered = repr({"signer": signer, "sealed": sealed})
    assert "PRIVATE KEY" not in rendered
    assert set(sealed["terminal_authority_proof"]) == {
        "authority_kind",
        "key_id",
        "public_key",
        "scheme",
        "signature",
    }


def test_host_coordinator_creates_private_local_key_without_ssh_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    signer = module.CoordinatorAuthority.from_local_state()
    sealed = signer.seal(terminal_record(), authority_kind="coordinator")

    module.verify_terminal_authority(
        sealed, registration=None, expected_kind="coordinator"
    )
    key_path = tmp_path / "dispatch-authority" / "coordinator-ed25519.pem"
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE KEY" not in repr(signer)


def test_host_coordinator_requires_kernel_process_memory_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ptrace_scope = tmp_path / "ptrace_scope"
    ptrace_scope.write_text("0\n", encoding="ascii")
    monkeypatch.setattr(module, "PTRACE_SCOPE_PATH", ptrace_scope)

    with pytest.raises(module.TerminalAuthorityError, match="process memory isolation"):
        module.CoordinatorAuthority.from_local_state()


def test_host_coordinator_provider_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(module, "_openssl", lambda: tmp_path / "openssl")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        ),
    )

    with pytest.raises(module.TerminalAuthorityError, match="timed out"):
        module._run_openssl([], input_bytes=b"")


def test_host_coordinator_verification_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer.seal(terminal_record(), authority_kind="dispatcher")
    module._signature_is_valid.cache_clear()
    observed: dict[str, object] = {}
    calls = 0

    def timeout(*args, **kwargs):
        nonlocal calls
        calls += 1
        observed.update(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", timeout)

    for _ in range(2):
        with pytest.raises(module.TerminalAuthorityError, match="timed out"):
            module.verify_terminal_authority(
                sealed,
                registration=signer.registration(),
                expected_kind="dispatcher",
            )

    assert observed["timeout"] == module.AUTHORITY_PROVIDER_TIMEOUT_S
    assert calls == 2


def test_host_coordinator_verifier_requires_kernel_process_memory_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    signer = module.CoordinatorAuthority.from_local_state()
    sealed = signer.seal(terminal_record(), authority_kind="coordinator")
    ptrace_scope = tmp_path / "ptrace_scope"
    ptrace_scope.write_text("0\n", encoding="ascii")
    monkeypatch.setattr(module, "PTRACE_SCOPE_PATH", ptrace_scope)

    with pytest.raises(
        module.TerminalAuthorityError, match="coordinator authority key is unavailable"
    ) as caught:
        module.verify_terminal_authority(
            sealed, registration=None, expected_kind="coordinator"
        )
    assert caught.value.__cause__ is not None
    assert "process memory isolation" in str(caught.value.__cause__)


def test_host_coordinator_proof_is_bound_to_local_trust_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    signer = module.CoordinatorAuthority.from_local_state()
    sealed = signer.seal(terminal_record(), authority_kind="coordinator")

    module.verify_terminal_authority(
        sealed, registration=None, expected_kind="coordinator"
    )
    proof = sealed["terminal_authority_proof"]
    assert proof["scheme"] == module.COORDINATOR_AUTHORITY_SCHEME
    assert proof["public_key"] == module.base64.b64encode(signer.public_key).decode(
        "ascii"
    )

    other_state = tmp_path / "other-authority"
    monkeypatch.setattr(module, "_coordinator_state_directory", lambda: other_state)
    module.CoordinatorAuthority.from_local_state()
    with pytest.raises(
        module.TerminalAuthorityError, match="coordinator authority key"
    ):
        module.verify_terminal_authority(
            sealed, registration=None, expected_kind="coordinator"
        )


def test_host_coordinator_cannot_seal_dispatcher_record() -> None:
    with pytest.raises(module.TerminalAuthorityError, match="cannot seal dispatcher"):
        module.CoordinatorAuthority.from_local_state().seal(
            terminal_record(), authority_kind="dispatcher"
        )


def test_host_coordinator_proof_binds_authority_kind() -> None:
    sealed = module.CoordinatorAuthority.from_local_state().seal(
        terminal_record(), authority_kind="coordinator"
    )
    sealed["terminal_authority_proof"]["authority_kind"] = "dispatcher"

    with pytest.raises(module.TerminalAuthorityError, match="authority kind"):
        module.verify_terminal_authority(
            sealed, registration=None, expected_kind="coordinator"
        )


def test_authority_rejects_unsigned_terminal() -> None:
    with pytest.raises(module.TerminalAuthorityError, match="proof"):
        module.verify_terminal_authority(
            terminal_record(), registration=None, expected_kind="coordinator"
        )
