import importlib.util
import json
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

    with pytest.raises(module.TerminalAuthorityError, match="scheme"):
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
    registration = signer.registration()
    registration["scheme"] = module.LEGACY_AUTHORITY_SCHEME

    with pytest.raises(module.TerminalAuthorityError, match="dispatcher"):
        module.verify_terminal_authority(
            sealed,
            registration=registration,
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


def test_ephemeral_terminal_key_cannot_self_assert_coordinator_authority() -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer.seal(
        terminal_record(), authority_kind="coordinator", include_public_key=True
    )

    with pytest.raises(module.TerminalAuthorityError, match="scheme"):
        module.verify_terminal_authority(
            sealed,
            registration=None,
            expected_kind="coordinator",
        )


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


def test_coordinator_ledger_state_round_trip_is_private_and_generation_guarded(
    tmp_path: Path,
) -> None:
    binding = "a" * 64
    initial = {
        "scheme": "dispatch-authority-ledger-host-state-v1",
        "repository_binding": binding,
        "ledger_id": "b" * 32,
        "generation": 1,
        "checkpoint_digest": "c" * 64,
    }

    module.commit_coordinator_ledger_state(binding, initial, expected_generation=None)

    assert module.load_coordinator_ledger_state(binding) == initial
    path = tmp_path / "dispatch-authority" / "ledgers" / f"{binding}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700

    advanced = {**initial, "generation": 2, "checkpoint_digest": "d" * 64}
    module.commit_coordinator_ledger_state(binding, advanced, expected_generation=1)
    assert module.load_coordinator_ledger_state(binding) == advanced

    with pytest.raises(module.TerminalAuthorityError, match="generation changed"):
        module.commit_coordinator_ledger_state(binding, advanced, expected_generation=1)


def test_coordinator_ledger_state_refuses_foreign_binding() -> None:
    binding = "a" * 64

    with pytest.raises(module.TerminalAuthorityError, match="binding"):
        module.commit_coordinator_ledger_state(
            binding,
            {
                "repository_binding": "b" * 64,
                "generation": 1,
            },
            expected_generation=None,
        )


def test_coordinator_ledger_state_refuses_initial_generation_skip() -> None:
    binding = "a" * 64

    with pytest.raises(module.TerminalAuthorityError, match="generation"):
        module.commit_coordinator_ledger_state(
            binding,
            {
                "scheme": "dispatch-authority-ledger-host-state-v1",
                "repository_binding": binding,
                "generation": 2,
            },
            expected_generation=None,
        )


def test_coordinator_ledger_state_refuses_unsafe_file(
    tmp_path: Path,
) -> None:
    binding = "a" * 64
    ledgers = tmp_path / "dispatch-authority" / "ledgers"
    ledgers.mkdir(parents=True, mode=0o700)
    ledgers.parent.chmod(0o700)
    ledgers.chmod(0o700)
    state_path = ledgers / f"{binding}.json"
    state_path.write_text("{}\n", encoding="utf-8")
    state_path.chmod(0o644)

    with pytest.raises(module.TerminalAuthorityError, match="state is unsafe"):
        module.load_coordinator_ledger_state(binding)


def test_coordinator_ledger_state_refuses_symlink(
    tmp_path: Path,
) -> None:
    binding = "a" * 64
    ledgers = tmp_path / "dispatch-authority" / "ledgers"
    ledgers.mkdir(parents=True, mode=0o700)
    ledgers.parent.chmod(0o700)
    ledgers.chmod(0o700)
    target = tmp_path / "foreign.json"
    target.write_text("{}\n", encoding="utf-8")
    (ledgers / f"{binding}.json").symlink_to(target)

    with pytest.raises(module.TerminalAuthorityError, match="state is unsafe"):
        module.load_coordinator_ledger_state(binding)


def test_coordinator_ledger_state_refuses_partial_json(
    tmp_path: Path,
) -> None:
    binding = "a" * 64
    ledgers = tmp_path / "dispatch-authority" / "ledgers"
    ledgers.mkdir(parents=True, mode=0o700)
    ledgers.parent.chmod(0o700)
    ledgers.chmod(0o700)
    state_path = ledgers / f"{binding}.json"
    state_path.write_text('{"generation":', encoding="utf-8")
    state_path.chmod(0o600)

    with pytest.raises(module.TerminalAuthorityError, match="state is invalid"):
        module.load_coordinator_ledger_state(binding)


def test_coordinator_ledger_state_cas_does_not_overwrite_malformed_current_state(
    tmp_path: Path,
) -> None:
    binding = "a" * 64
    ledgers = tmp_path / "dispatch-authority" / "ledgers"
    ledgers.mkdir(parents=True, mode=0o700)
    ledgers.parent.chmod(0o700)
    ledgers.chmod(0o700)
    state_path = ledgers / f"{binding}.json"
    malformed = {
        "scheme": "foreign-state-v1",
        "repository_binding": binding,
        "generation": 1,
    }
    state_path.write_text(json.dumps(malformed), encoding="utf-8")
    state_path.chmod(0o600)
    replacement = {
        "scheme": "dispatch-authority-ledger-host-state-v1",
        "repository_binding": binding,
        "generation": 2,
    }

    with pytest.raises(module.TerminalAuthorityError, match="state is invalid"):
        module.commit_coordinator_ledger_state(
            binding, replacement, expected_generation=1
        )

    assert json.loads(state_path.read_text(encoding="utf-8")) == malformed


def test_host_verifier_caches_only_derived_public_material(monkeypatch) -> None:
    signer = module.CoordinatorAuthority.from_local_state()
    sealed = signer.seal(terminal_record(), authority_kind="coordinator")
    module._trusted_public_key_from_file.cache_clear()
    original_read = module._read_private_key
    reads = 0

    def counted_read(path: Path) -> bytes:
        nonlocal reads
        reads += 1
        return original_read(path)

    monkeypatch.setattr(module, "_read_private_key", counted_read)

    for _ in range(2):
        module.verify_terminal_authority(
            sealed, registration=None, expected_kind="coordinator"
        )

    assert reads == 1
    assert not hasattr(module._public_key_from_private, "cache_info")


def test_private_key_reader_uses_validated_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module.CoordinatorAuthority.from_local_state()
    key_path = module._coordinator_state_directory() / module.COORDINATOR_KEY_FILENAME

    def reject_path_read(_path: Path) -> bytes:
        pytest.fail("private key must be read through its validated descriptor")

    monkeypatch.setattr(Path, "read_bytes", reject_path_read)

    assert b"PRIVATE KEY" in module._read_private_key(key_path)


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


def test_host_coordinator_verification_provider_failure_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = module.TerminalAuthority.generate()
    sealed = signer.seal(terminal_record(), authority_kind="dispatcher")
    module._signature_is_valid.cache_clear()
    calls = 0

    def provider_failure(*args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(args[0], 2, stdout=b"", stderr=b"failed")

    monkeypatch.setattr(module.subprocess, "run", provider_failure)

    for _ in range(2):
        with pytest.raises(module.TerminalAuthorityError, match="provider failed"):
            module.verify_terminal_authority(
                sealed,
                registration=signer.registration(),
                expected_kind="dispatcher",
            )

    assert calls == 2


def test_legacy_coordinator_verification_provider_failure_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module._legacy_coordinator_signature_is_valid.cache_clear()
    calls = 0

    def provider_failure(*args, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(args[0], 2, stdout=b"", stderr=b"failed")

    monkeypatch.setattr(module.subprocess, "run", provider_failure)

    for _ in range(2):
        with pytest.raises(module.TerminalAuthorityError, match="provider failed"):
            module._legacy_coordinator_signature_is_valid(b"signature", b"payload")

    assert calls == 2


def test_legacy_coordinator_invalid_signature_uses_real_cli_mismatch_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_path = tmp_path / "legacy-key"
    payload_path = tmp_path / "payload"
    payload_path.write_bytes(b"signed payload")
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "sign",
            "-f",
            str(key_path),
            "-n",
            module.LEGACY_COORDINATOR_SIGNATURE_NAMESPACE,
            str(payload_path),
        ],
        check=True,
        capture_output=True,
    )
    public_key = " ".join(
        (tmp_path / "legacy-key.pub").read_text(encoding="ascii").split()[:2]
    )
    signature = (tmp_path / "payload.sig").read_bytes()
    monkeypatch.setattr(module, "LEGACY_COORDINATOR_PUBLIC_KEY", public_key)
    module._legacy_coordinator_signature_is_valid.cache_clear()
    real_run = subprocess.run
    calls = 0

    def counted_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", counted_run)

    assert not module._legacy_coordinator_signature_is_valid(
        signature, b"altered payload"
    )
    assert not module._legacy_coordinator_signature_is_valid(
        signature, b"altered payload"
    )
    assert calls == 1


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


def test_legacy_coordinator_proof_is_available_only_to_explicit_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = terminal_record()
    proof = {
        "scheme": module.LEGACY_COORDINATOR_AUTHORITY_SCHEME,
        "authority_kind": "coordinator",
        "key_id": module._legacy_coordinator_key_id(
            module.LEGACY_COORDINATOR_PUBLIC_KEY
        ),
        "public_key": module.LEGACY_COORDINATOR_PUBLIC_KEY,
        "signature": module.base64.b64encode(b"legacy-signature").decode("ascii"),
    }
    sealed = {**record, "terminal_authority_proof": proof}
    observed: dict[str, bytes] = {}

    def verify(signature: bytes, payload: bytes) -> bool:
        observed["signature"] = signature
        observed["payload"] = payload
        return True

    monkeypatch.setattr(module, "_legacy_coordinator_signature_is_valid", verify)

    module.verify_legacy_coordinator_authority(sealed)

    assert observed["signature"] == b"legacy-signature"
    assert observed["payload"] == module._canonical_payload(
        sealed,
        authority_kind="coordinator",
        scheme=module.LEGACY_COORDINATOR_AUTHORITY_SCHEME,
    )
    with pytest.raises(module.TerminalAuthorityError, match="scheme"):
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
