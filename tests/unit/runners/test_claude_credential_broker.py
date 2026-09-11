"""Focused tests for the trusted host-side Claude credential broker."""

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def _load_broker():
    from loopzero.runners import claude as claude_credential

    return claude_credential


claude_credential = _load_broker()


REQUIRED_SCOPES = [
    "user:inference",
    "user:profile",
    "user:sessions:claude_code",
]


def _credential(*, expires_at_ms: int, access_token: str = "access") -> dict:
    return {
        "claudeAiOauth": {
            "accessToken": access_token,
            "refreshToken": "refresh",
            "expiresAt": expires_at_ms,
            "refreshTokenExpiresAt": 10**15,
            "scopes": REQUIRED_SCOPES,
            "subscriptionType": "max",
        }
    }


def _write_credential(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _runtime_snapshot(payload: dict) -> dict:
    oauth = payload["claudeAiOauth"]
    return {
        "claudeAiOauth": {
            "accessToken": oauth["accessToken"],
            "refreshToken": oauth["accessToken"],
            "expiresAt": oauth["expiresAt"],
            "refreshTokenExpiresAt": oauth["expiresAt"],
            "scopes": sorted(oauth["scopes"]),
            "subscriptionType": oauth["subscriptionType"].strip().lower(),
        }
    }


def test_fresh_credential_is_snapshotted_without_refresh(tmp_path: Path) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    payload = _credential(expires_at_ms=2_000_000)
    _write_credential(credential, payload)

    def forbidden_refresh(*_args, **_kwargs):
        raise AssertionError("a credential beyond the requested horizon is fresh")

    with claude_credential.claude_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1_000.0,
        run_status=forbidden_refresh,
    ) as descriptor:
        snapshot = os.pread(descriptor, 1024 * 1024, 0)
        assert json.loads(snapshot) == _runtime_snapshot(payload)
        assert json.loads(snapshot)["claudeAiOauth"]["refreshToken"] != "refresh"
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o222 == 0

    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_near_expiry_refreshes_in_host_staging_and_installs_validated_snapshot(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    original = _credential(expires_at_ms=1_100_000, access_token="old")
    refreshed = _credential(expires_at_ms=3_000_000, access_token="new")
    _write_credential(credential, original)
    assert claude_credential.BROKER_LOCK_NAME == ".oauth_refresh.lock"
    lock = credential.parent / claude_credential.BROKER_LOCK_NAME
    lock.touch(mode=0o644)
    lock.chmod(0o644)
    observed_lock: dict[str, object] = {}

    def refresh(command, *, timeout, check, capture_output, text, env, cwd):
        assert command[-3:] == ["auth", "status", "--json"]
        assert timeout == claude_credential.REFRESH_TIMEOUT_S
        assert check is False
        assert capture_output is True
        assert text is True
        assert cwd == Path(env["HOME"])
        staging_credential = Path(env["HOME"]) / ".claude" / ".credentials.json"
        staged = json.loads(staging_credential.read_text(encoding="utf-8"))
        assert staged["claudeAiOauth"]["expiresAt"] < 1_000_000
        _write_credential(staging_credential, refreshed)
        observed_lock["mode"] = stat.S_IMODE(lock.stat().st_mode)
        observed_lock["uid"] = lock.stat().st_uid
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "subscriptionType": "max",
                }
            ),
            stderr="",
        )

    with claude_credential.claude_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1_000.0,
        run_status=refresh,
        claude_binary=Path("/trusted/claude"),
    ) as descriptor:
        snapshot = json.loads(os.pread(descriptor, 1024 * 1024, 0))
        assert snapshot == _runtime_snapshot(refreshed)

    assert json.loads(credential.read_text(encoding="utf-8")) == refreshed
    assert stat.S_IMODE(credential.stat().st_mode) == 0o600
    assert observed_lock == {"mode": 0o600, "uid": os.getuid()}


def test_refresh_accepts_claude_binary_rewrite_with_same_validated_oauth_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    original = _credential(expires_at_ms=1_100_000, access_token="old")
    refreshed = _credential(expires_at_ms=3_000_000, access_token="new")
    rewritten = json.loads(json.dumps(refreshed))
    rewritten["claudeAiOauth"]["subscriptionType"] = " MAX "
    rewritten["claudeAiOauth"]["scopes"] = list(reversed(REQUIRED_SCOPES))
    rewritten["claudeAiOauth"]["futureSecret"] = "must-not-reach-sandbox"
    rewritten["providerMetadata"] = {"rewrittenBy": "claude"}
    _write_credential(credential, original)

    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
import tempfile
from pathlib import Path

host = Path({str(credential)!r})
if sys.argv[1] == "rewrite-installed":
    host.write_text(
        json.dumps({rewritten!r}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    host.chmod(0o600)
    raise SystemExit(0)

source = json.loads(host.read_text(encoding="utf-8"))
metadata = source.setdefault("providerMetadata", {{}})
metadata["rewriteCount"] = metadata.get("rewriteCount", 0) + 1
host.write_text(json.dumps(source, indent=2, sort_keys=True), encoding="utf-8")
host.chmod(0o600)

staging = Path(os.environ[\"HOME\"]) / \".claude\" / \".credentials.json\"
staging.write_text(json.dumps({refreshed!r}), encoding=\"utf-8\")
staging.chmod(0o600)
print(json.dumps({{\"loggedIn\": True}}))
""",
        encoding="utf-8",
    )
    fake_claude.chmod(0o700)

    install_credential = claude_credential._install_credential

    def install_and_rewrite(path, candidate):
        install_credential(path, candidate)
        subprocess.run(
            [fake_claude, "rewrite-installed"],
            check=True,
            capture_output=True,
            text=True,
        )

    monkeypatch.setattr(
        claude_credential,
        "_install_credential",
        install_and_rewrite,
    )

    with claude_credential.claude_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1_000.0,
        claude_binary=fake_claude,
    ) as descriptor:
        snapshot = json.loads(os.pread(descriptor, 1024 * 1024, 0))

    assert json.loads(credential.read_text(encoding="utf-8")) == rewritten
    assert snapshot == _runtime_snapshot(rewritten)


def test_refresh_race_is_not_reported_as_unsafe_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    original = _credential(expires_at_ms=1_100_000, access_token="old")
    refreshed = _credential(expires_at_ms=3_000_000, access_token="new")
    raced = _credential(expires_at_ms=4_000_000, access_token="concurrent-login")
    _write_credential(credential, original)

    def refresh(command, *, env, **_kwargs):
        staging_credential = Path(env["HOME"]) / ".claude" / ".credentials.json"
        _write_credential(staging_credential, refreshed)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"loggedIn": True}),
            stderr="",
        )

    install_credential = claude_credential._install_credential

    def install_then_race(path, candidate):
        install_credential(path, candidate)
        _write_credential(path, raced)

    monkeypatch.setattr(claude_credential, "_install_credential", install_then_race)

    with pytest.raises(
        claude_credential.ClaudeCredentialRefreshFailed,
        match="refresh race during trusted installation",
    ):
        with claude_credential.claude_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
            run_status=refresh,
            claude_binary=Path("/trusted/claude"),
        ):
            pass


def test_concurrent_login_replaces_refresh_source_without_being_overwritten(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    original = _credential(expires_at_ms=1_100_000, access_token="old")
    refreshed = _credential(expires_at_ms=3_000_000, access_token="refreshed-old")
    concurrent_login = _credential(
        expires_at_ms=4_000_000,
        access_token="concurrent-login",
    )
    _write_credential(credential, original)

    def refresh(command, *, env, **_kwargs):
        _write_credential(credential, concurrent_login)
        staging_credential = Path(env["HOME"]) / ".claude" / ".credentials.json"
        _write_credential(staging_credential, refreshed)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"loggedIn": True}),
            stderr="",
        )

    with claude_credential.claude_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1_000.0,
        run_status=refresh,
        claude_binary=Path("/trusted/claude"),
    ) as descriptor:
        snapshot = json.loads(os.pread(descriptor, 1024 * 1024, 0))

    assert json.loads(credential.read_text(encoding="utf-8")) == concurrent_login
    assert snapshot == _runtime_snapshot(concurrent_login)


def test_concurrent_near_expiry_login_is_refreshed_from_its_own_lineage(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    original = _credential(expires_at_ms=1_100_000, access_token="old")
    concurrent_login = _credential(
        expires_at_ms=1_200_000,
        access_token="concurrent-login",
    )
    discarded_refresh = _credential(
        expires_at_ms=3_000_000,
        access_token="discarded-refresh",
    )
    final_refresh = _credential(
        expires_at_ms=4_000_000,
        access_token="refreshed-concurrent-login",
    )
    _write_credential(credential, original)
    refreshed_sources: list[str] = []

    def refresh(command, *, env, **_kwargs):
        staging_credential = Path(env["HOME"]) / ".claude" / ".credentials.json"
        staged = json.loads(staging_credential.read_text(encoding="utf-8"))
        source = staged["claudeAiOauth"]["accessToken"]
        refreshed_sources.append(source)
        if source == "old":
            _write_credential(credential, concurrent_login)
            candidate = discarded_refresh
        else:
            candidate = final_refresh
        _write_credential(staging_credential, candidate)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"loggedIn": True}),
            stderr="",
        )

    with claude_credential.claude_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1_000.0,
        run_status=refresh,
        claude_binary=Path("/trusted/claude"),
    ) as descriptor:
        snapshot = json.loads(os.pread(descriptor, 1024 * 1024, 0))

    assert refreshed_sources == ["old", "concurrent-login"]
    assert json.loads(credential.read_text(encoding="utf-8")) == final_refresh
    assert snapshot == _runtime_snapshot(final_refresh)


def test_repeated_concurrent_logins_fail_bounded_without_overwriting_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    original = _credential(expires_at_ms=1_100_000, access_token="original")
    replacements = [
        _credential(expires_at_ms=1_200_000, access_token="concurrent-one"),
        _credential(expires_at_ms=1_300_000, access_token="concurrent-two"),
    ]
    _write_credential(credential, original)
    refresh_calls = 0
    resolve_calls = 0

    def resolve_binary() -> Path:
        nonlocal resolve_calls
        resolve_calls += 1
        return Path("/trusted/claude")

    monkeypatch.setattr(claude_credential, "_resolve_claude_binary", resolve_binary)

    def refresh(command, *, env, **_kwargs):
        nonlocal refresh_calls
        replacement = replacements[refresh_calls]
        refresh_calls += 1
        _write_credential(credential, replacement)
        staging_credential = Path(env["HOME"]) / ".claude" / ".credentials.json"
        _write_credential(
            staging_credential,
            _credential(
                expires_at_ms=3_000_000 + refresh_calls,
                access_token=f"discarded-{refresh_calls}",
            ),
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"loggedIn": True}),
            stderr="",
        )

    with pytest.raises(
        claude_credential.ClaudeCredentialRefreshFailed,
        match="changed during trusted refresh",
    ):
        with claude_credential.claude_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
            run_status=refresh,
        ):
            pass

    assert refresh_calls == claude_credential.MAX_REFRESH_ATTEMPTS
    assert resolve_calls == 1
    assert json.loads(credential.read_text(encoding="utf-8")) == replacements[-1]


def test_refresh_timeout_is_distinct_and_preserves_host_credential(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    original = _credential(expires_at_ms=1_100_000)
    _write_credential(credential, original)

    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 30)

    with pytest.raises(claude_credential.ClaudeCredentialRefreshTimeout):
        with claude_credential.claude_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
            run_status=timeout,
            claude_binary=Path("/trusted/claude"),
        ):
            pass

    assert json.loads(credential.read_text(encoding="utf-8")) == original


def test_default_refresh_runner_kills_the_entire_process_group_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    killed: list[tuple[int, signal.Signals]] = []
    observed: dict[str, object] = {}

    class FakePipe:
        closed = False

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        pid = 4242
        returncode = -signal.SIGKILL
        calls = 0
        stdout = FakePipe()
        stderr = FakePipe()

        def communicate(self, *, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["claude"], timeout)
            assert timeout == claude_credential.PROCESS_REAP_TIMEOUT_S
            raise subprocess.TimeoutExpired(["claude"], timeout)

        def poll(self):
            return None

        def wait(self, *, timeout=None):
            observed["wait_timeout"] = timeout
            return self.returncode

    def popen(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(claude_credential.subprocess, "Popen", popen)
    monkeypatch.setattr(
        claude_credential.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    with pytest.raises(subprocess.TimeoutExpired):
        claude_credential._run_refresh_process_group(
            ["claude", "auth", "status", "--json"],
            timeout=1,
            check=False,
            capture_output=True,
            text=True,
            env={},
            cwd=tmp_path,
        )

    assert killed == [(4242, signal.SIGKILL)]
    assert observed["wait_timeout"] == claude_credential.PROCESS_REAP_TIMEOUT_S
    assert FakeProcess.stdout.closed is True
    assert FakeProcess.stderr.closed is True
    assert observed["kwargs"] == {
        "cwd": tmp_path,
        "env": {},
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "start_new_session": True,
    }


@pytest.mark.parametrize("use_memfd", [True, False], ids=["memfd", "temporary-file"])
def test_snapshot_descriptor_retries_short_writes(
    monkeypatch: pytest.MonkeyPatch,
    use_memfd: bool,
) -> None:
    if use_memfd and not hasattr(claude_credential.os, "memfd_create"):
        pytest.skip("memfd_create is unavailable on this Python platform")
    payload = b'{"credential":"complete-payload"}'
    original_write = os.write

    def short_write(descriptor: int, remaining: bytes) -> int:
        limit = max(1, len(remaining) // 2)
        return original_write(descriptor, remaining[:limit])

    monkeypatch.setattr(claude_credential.os, "write", short_write)
    if not use_memfd:
        monkeypatch.delattr(claude_credential.os, "memfd_create", raising=False)

    descriptor = claude_credential._snapshot_descriptor(payload)
    try:
        assert os.pread(descriptor, len(payload) + 1, 0) == payload
    finally:
        os.close(descriptor)


def test_revoked_status_is_distinct_and_preserves_host_credential(
    tmp_path: Path,
) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    original = _credential(expires_at_ms=1_100_000)
    _write_credential(credential, original)

    def revoked(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps(
                {
                    "loggedIn": False,
                    "authMethod": "claude.ai",
                    "subscriptionType": "max",
                }
            ),
            stderr="private vendor detail",
        )

    with pytest.raises(claude_credential.ClaudeCredentialRevoked):
        with claude_credential.claude_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
            run_status=revoked,
            claude_binary=Path("/trusted/claude"),
        ):
            pass

    assert json.loads(credential.read_text(encoding="utf-8")) == original


@pytest.mark.parametrize(
    "mutation",
    [
        lambda oauth: oauth.update(expiresAt=1_200_000),
        lambda oauth: oauth.update(scopes=["user:profile"]),
    ],
    ids=["expiry-not-advanced", "required-scope-missing"],
)
def test_refresh_rejects_unadvanced_or_under_scoped_credential(
    tmp_path: Path, mutation
) -> None:
    credential = tmp_path / ".claude" / ".credentials.json"
    original = _credential(expires_at_ms=1_100_000)
    candidate = _credential(expires_at_ms=3_000_000, access_token="candidate")
    mutation(candidate["claudeAiOauth"])
    _write_credential(credential, original)

    def invalid_refresh(command, *, env, **_kwargs):
        staging_credential = Path(env["HOME"]) / ".claude" / ".credentials.json"
        _write_credential(staging_credential, candidate)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "subscriptionType": "max",
                }
            ),
            stderr="",
        )

    with pytest.raises(claude_credential.ClaudeCredentialError):
        with claude_credential.claude_subscription_credential(
            requested_runtime_s=600,
            credential_path=credential,
            clock=lambda: 1_000.0,
            run_status=invalid_refresh,
            claude_binary=Path("/trusted/claude"),
        ):
            pass

    assert json.loads(credential.read_text(encoding="utf-8")) == original


@pytest.fixture
def token_root():
    # Host /tmp may itself be a Git checkout during concurrent tooling tests.
    # Use an independent private Linux tmpfs directory for the host-only source.
    with tempfile.TemporaryDirectory(
        prefix="claude-token-test-", dir="/dev/shm"
    ) as directory:
        yield Path(directory)


@pytest.fixture(autouse=True)
def isolated_default_token(monkeypatch, token_root, request):
    from loopzero.runners import claude as claude_token

    if request.node.name.startswith("test_default_path_"):
        return
    path = token_root / "account" / ".local" / "state" / "intelflo" / "claude-token"
    monkeypatch.setattr(
        claude_token, "_default_token_path", lambda: path, raising=False
    )
    return path


@pytest.fixture
def token_broker(monkeypatch):
    from loopzero.runners import claude as claude_token

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("INTELFLO_CLAUDE_TOKEN_FILE", raising=False)
    monkeypatch.setattr(claude_token, "_validate_remote_token", lambda token: None)
    return claude_token


@pytest.mark.parametrize("source", ["token-env", "token-file", "token-file(default)"])
def test_long_lived_token_fallback_is_sealed_and_not_installed(
    token_root, monkeypatch, token_broker, source, isolated_default_token
):
    token = "sk-ant-oat01-" + "x" * 80
    credential = token_root / ".claude" / ".credentials.json"
    _write_credential(credential, _credential(expires_at_ms=1_100_000))
    original = credential.read_bytes()
    if source == "token-env":
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    else:
        token_file = (
            isolated_default_token
            if source == "token-file(default)"
            else token_root / "token"
        )
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token + "\n")
        token_file.chmod(0o600)
        if source == "token-file":
            monkeypatch.setenv("INTELFLO_CLAUDE_TOKEN_FILE", str(token_file))

    def failed_refresh(*args, **kwargs):
        raise claude_credential.ClaudeCredentialRefreshFailed("refresh failed")

    monkeypatch.setattr(claude_credential, "_refresh_credential", failed_refresh)
    with claude_credential.claude_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1000,
        claude_binary=Path("/unused"),
    ) as fd:
        assert token_broker.snapshot_source(fd) == source
        assert token_broker.snapshot_token(fd) == token
        with pytest.raises(OSError):
            os.write(fd, b"change")
        assert credential.read_bytes() == original
    with pytest.raises(OSError):
        os.fstat(fd)


def test_refreshable_oauth_wins_over_token(token_root, monkeypatch, token_broker):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "malformed")
    credential = token_root / ".credentials.json"
    _write_credential(credential, _credential(expires_at_ms=2_000_000))
    with claude_credential.claude_subscription_credential(
        requested_runtime_s=600,
        credential_path=credential,
        clock=lambda: 1000,
    ) as fd:
        assert token_broker.snapshot_source(fd) == "oauth-file"
        assert token_broker.snapshot_token(fd) is None


@pytest.mark.parametrize(
    "token",
    ["", "bad", "sk-ant-api03-" + "x" * 80, "sk-ant-oat01-" + "x" * 80 + "\nextra"],
)
def test_malformed_fallback_refused(token_root, monkeypatch, token_broker, token):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    with pytest.raises(claude_credential.ClaudeCredentialError):
        with claude_credential.claude_subscription_credential(
            requested_runtime_s=600,
            credential_path=token_root / "missing",
        ):
            pytest.fail("malformed token admitted")


@pytest.mark.parametrize("kind", ["permissions", "symlink", "expired", "network"])
def test_unsafe_or_unverifiable_token_refused(
    token_root, monkeypatch, token_broker, kind
):
    token_file = token_root / "token"
    token_file.write_text("sk-ant-oat01-" + "x" * 80)
    token_file.chmod(0o644 if kind == "permissions" else 0o600)
    if kind == "symlink":
        link = token_root / "link"
        link.symlink_to(token_file)
        token_file = link
    if kind in {"expired", "network"}:

        def reject(token):
            raise claude_credential.ClaudeCredentialUnavailable(
                "token validation failed"
            )

        monkeypatch.setattr(token_broker, "_validate_remote_token", reject)
    monkeypatch.setenv("INTELFLO_CLAUDE_TOKEN_FILE", str(token_file))
    with pytest.raises(claude_credential.ClaudeCredentialError):
        with claude_credential.claude_subscription_credential(
            requested_runtime_s=600,
            credential_path=token_root / "missing",
        ):
            pytest.fail("unsafe token admitted")


@pytest.fixture
def token_count_response():
    """Synthetic token-count response; never a captured credential or identity."""
    return (Path(__file__).parent / "fixtures" / "claude_token_count.json").read_bytes()


@pytest.mark.parametrize(
    "status,payload,accepted",
    [
        (200, None, True),
        (201, None, True),
        (401, None, False),
        (403, None, False),
        (302, None, False),
        (500, None, False),
        (204, b"", False),
        (200, b"{}", False),
        (200, b'{"input_tokens": true}', False),
        (200, b'{"input_tokens": -1}', False),
        (200, b'{"input_tokens": "8"}', False),
        (200, b'{"input_tokens": 1.5}', False),
        (200, b"not-json", False),
        (200, b" " * 16385, False),
    ],
)
def test_remote_token_validation_is_pinned_and_fail_closed(
    monkeypatch, token_count_response, status, payload, accepted
):
    from loopzero.runners import claude as claude_token

    class Connection:
        closed = False

        def __init__(self, host, *, timeout):
            assert host == "api.anthropic.com"
            assert timeout == 10

        def request(self, method, path, *, body, headers):
            assert (method, path) == ("POST", "/v1/messages/count_tokens?beta=true")
            assert json.loads(body) == {
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "."}],
            }
            assert headers == {
                "Authorization": "Bearer test-only",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20,token-counting-2024-11-01",
            }

        def getresponse(self):
            return self

        def read(self, limit):
            assert limit == 16385
            assert 200 <= self.status < 300
            return token_count_response if payload is None else payload

        def close(self):
            Connection.closed = True

    Connection.status = status
    monkeypatch.setattr(claude_token.http.client, "HTTPSConnection", Connection)
    if accepted:
        claude_token._validate_remote_token("test-only")
    else:
        with pytest.raises(claude_credential.ClaudeCredentialUnavailable):
            claude_token._validate_remote_token("test-only")
    assert Connection.closed


def test_token_status_requires_broker_validated_snapshot():
    from loopzero.runners.claude import ClaudeProtocolError, parse_claude_auth_status

    status = json.dumps({"loggedIn": True, "authMethod": "oauth_token"})
    with pytest.raises(ClaudeProtocolError):
        parse_claude_auth_status(status)
    assert parse_claude_auth_status(status, allow_validated_token=True).logged_in


@pytest.mark.parametrize("kind", ["worktree", "parent-symlink", "hard-link"])
def test_token_file_cannot_be_exposed_through_worktree_or_alias(
    token_root, monkeypatch, token_broker, kind
):
    host = token_root / "private"
    host.mkdir(mode=0o700)
    worktree = token_root / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /unused")
    token_file = (worktree if kind == "worktree" else host) / "token"
    token_file.write_text("sk-ant-oat01-" + "x" * 80)
    token_file.chmod(0o600)
    if kind == "parent-symlink":
        alias = token_root / "alias"
        alias.symlink_to(host, target_is_directory=True)
        token_file = alias / "token"
    elif kind == "hard-link":
        os.link(token_file, worktree / "token")
    monkeypatch.setenv("INTELFLO_CLAUDE_TOKEN_FILE", str(token_file))
    with pytest.raises(claude_credential.ClaudeCredentialError):
        token_broker.token_snapshot()


@pytest.mark.parametrize("source", ["token-env", "token-file"])
@pytest.mark.parametrize("valid", [True, False])
def test_explicit_token_source_wins_over_default(
    token_root, monkeypatch, token_broker, isolated_default_token, source, valid
):
    isolated_default_token.parent.mkdir(parents=True)
    isolated_default_token.write_text("sk-ant-oat01-" + "d" * 80 + "\n")
    isolated_default_token.chmod(0o600)
    explicit = "sk-ant-oat01-" + "e" * 80 if valid else ""
    if source == "token-env":
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", explicit)
    else:
        path = token_root / "explicit"
        path.write_text(explicit)
        path.chmod(0o600)
        monkeypatch.setenv("INTELFLO_CLAUDE_TOKEN_FILE", str(path))
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-" + "e" * 80)
    if not valid:
        with pytest.raises(claude_credential.ClaudeCredentialError):
            token_broker.token_snapshot()
        return
    fd = token_broker.token_snapshot()
    try:
        assert token_broker.snapshot_source(fd) == source
        assert token_broker.snapshot_token(fd) == explicit
    finally:
        os.close(fd)


def test_default_absent_preserves_oauth_failure(token_root, token_broker):
    assert token_broker.token_snapshot() is None
    with pytest.raises(claude_credential.ClaudeCredentialUnavailable):
        with claude_credential.claude_subscription_credential(
            requested_runtime_s=600, credential_path=token_root / "missing"
        ):
            pytest.fail("missing credentials admitted")


@pytest.mark.parametrize(
    "kind",
    [
        "permissions",
        "symlink",
        "parent-symlink",
        "hard-link",
        "worktree",
        "directory",
        "malformed",
        "owner",
        "expired",
    ],
)
def test_unsafe_default_token_refused(
    token_root, monkeypatch, token_broker, isolated_default_token, kind
):
    path = isolated_default_token
    path.parent.mkdir(parents=True)
    path.write_text("bad" if kind == "malformed" else "sk-ant-oat01-" + "x" * 80)
    path.chmod(0o644 if kind == "permissions" else 0o600)
    if kind == "symlink":
        path.unlink()
        path.symlink_to(token_root / "missing")
    elif kind == "parent-symlink":
        directory = path.parent
        target = directory.with_name("other")
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    elif kind == "hard-link":
        os.link(path, token_root / "alias")
    elif kind == "worktree":
        (path.parent / ".git").write_text("gitdir: /unused")
    elif kind == "directory":
        path.unlink()
        path.mkdir(mode=0o600)
    elif kind == "owner":
        monkeypatch.setattr(
            claude_credential.os, "getuid", lambda: os.stat(path).st_uid + 1
        )
    elif kind == "expired":

        def reject(token):
            raise claude_credential.ClaudeCredentialUnavailable("validation failed")

        monkeypatch.setattr(token_broker, "_validate_remote_token", reject)
    with pytest.raises(claude_credential.ClaudeCredentialError):
        token_broker.token_snapshot()


def test_default_path_uses_account_home_not_environment(monkeypatch, token_broker):
    from types import SimpleNamespace

    monkeypatch.setattr(
        token_broker.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir="/account")
    )
    monkeypatch.setenv("HOME", "/untrusted")
    monkeypatch.setenv("XDG_STATE_HOME", "/untrusted-state")
    assert token_broker._default_token_path() == Path(
        "/account/.local/state/intelflo/claude-token"
    )


@pytest.mark.parametrize("home", ["relative", "/"])
def test_default_path_refuses_invalid_account_home(monkeypatch, token_broker, home):
    from types import SimpleNamespace

    monkeypatch.setattr(
        token_broker.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=home)
    )
    with pytest.raises(claude_credential.UnsafeClaudeCredential):
        token_broker._default_token_path()
