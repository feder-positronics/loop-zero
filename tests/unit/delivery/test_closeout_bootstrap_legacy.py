"""Security-bootstrap seam tests for the privileged closeout launcher."""

from __future__ import annotations

import ast
import fcntl
import importlib.util
import inspect
import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parent / "fixtures/closeout_root"
MODULE_PATH = Path(__file__).resolve().parents[3] / "src/loopzero/delivery/closeout.py"
SPEC = importlib.util.spec_from_file_location("pr_closeout_bootstrap", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)
from loopzero.kernel import git_config_security


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["/usr/bin/git", "-C", str(repo), *args], text=True
    ).strip()


def _init_repo(path: Path) -> None:
    subprocess.run(["/usr/bin/git", "init", "-q", str(path)], check=True)
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "Tests")


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def test_isolated_environment_is_an_allowlist_with_pinned_identity(
    tmp_path: Path,
) -> None:
    home = tmp_path / "operator-home"
    source = {
        "GH_TOKEN": "token",
        "GITHUB_TOKEN": "alternate-token",
        "TERM": "xterm-256color",
        "PATH": str(tmp_path / "attacker-bin"),
        "HOME": str(tmp_path / "attacker-home"),
        "GH_CONFIG_DIR": str(tmp_path / "attacker-gh"),
        "TMPDIR": str(tmp_path / "attacker-tmp"),
        "LD_PRELOAD": str(tmp_path / "evil.so"),
        "SHELLOPTS": "xtrace",
        "PS4": "$(touch /tmp/closeout-pwned)",
        "BASH_ENV": str(tmp_path / "bash-env"),
        "GIT_CONFIG_COUNT": "1",
    }

    environment = module.isolated_environment(source, home=home, user="operator")

    assert environment == {
        "GH_TOKEN": "token",
        "GITHUB_TOKEN": "alternate-token",
        "TERM": "xterm-256color",
        "HOME": str(home),
        "USER": "operator",
        "LOGNAME": "operator",
        "GH_CONFIG_DIR": str(home / ".config/gh"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": "/tmp",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_closeout_environment_preserves_canonical_job_authority(
    tmp_path: Path,
) -> None:
    environment = module.closeout_environment(
        module.isolated_environment({}, home=tmp_path / "home", user="operator"),
        primary=tmp_path / "primary",
        delivery=tmp_path / "delivery",
        revision="a" * 40,
        remote_default_ref="refs/heads/main",
        tree_oid="b" * 40,
    )

    assert "INTELFLO_JOB_DIR" not in environment
    assert environment["INTELFLO_DELIVERY_ROOT"] == str(tmp_path / "delivery")
    assert environment["INTELFLO_TRUSTED_PRIMARY"] == str(tmp_path / "primary")


def test_parse_launcher_arguments_separates_audited_rollback() -> None:
    revision = "a" * 40

    options, forwarded = module.parse_launcher_arguments(
        [
            "--pr",
            "123",
            "--trusted-revision",
            revision,
            "--rollback-reason",
            "recover current main regression",
            "--merge",
        ]
    )

    assert options.revision == revision
    assert options.rollback_reason == "recover current main regression"
    assert forwarded == ["--pr", "123", "--merge"]


@pytest.mark.parametrize("reserved", ["--origin-url", "--git-config-sha256"])
def test_launcher_rejects_candidate_override_of_repository_binding(
    reserved: str,
) -> None:
    with pytest.raises(module.BootstrapError, match="managed by the trusted launcher"):
        module.parse_launcher_arguments([reserved, "attacker-controlled"])


def test_shell_handoff_carries_validated_repository_binding() -> None:
    binding = module.RepositoryBinding("https://github.com/example/repo.git", "b" * 64)

    command = module.implementation_command(
        Path("/trusted-snapshot"), ["--pr", "123"], binding
    )

    assert command == [
        "/bin/bash",
        "-p",
        "/trusted-snapshot/scripts/util/pr_closeout_impl.sh",
        "--pr",
        "123",
        "--origin-url",
        binding.origin_url,
        "--git-config-sha256",
        binding.config_sha256,
    ]


def test_inline_python_cannot_import_candidate_shadow_modules(tmp_path: Path) -> None:
    marker = tmp_path / "candidate-json-imported"
    (tmp_path / "json.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('owned')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-c", "import json; json.dumps({'safe': True})"],
        cwd=tmp_path,
        env=module.isolated_environment(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_isolated_shell_ignores_bash_env_and_attacker_path(tmp_path: Path) -> None:
    startup_marker = tmp_path / "bash-env-executed"
    path_marker = tmp_path / "attacker-git-executed"
    bash_env = tmp_path / "bash_env.sh"
    attacker_bin = tmp_path / "bin"
    attacker_bin.mkdir()
    bash_env.write_text(f'touch "{startup_marker}"\n', encoding="utf-8")
    attacker_git = attacker_bin / "git"
    attacker_git.write_text(f'#!/bin/sh\ntouch "{path_marker}"\n', encoding="utf-8")
    attacker_git.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", "-p", "-c", "git --version >/dev/null"],
        env=module.isolated_environment(
            {
                **os.environ,
                "BASH_ENV": str(bash_env),
                "PATH": str(attacker_bin),
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not startup_marker.exists()
    assert not path_marker.exists()


def test_private_shell_forces_safe_python_and_pinned_bash() -> None:
    shell = (ROOT / "scripts/util/pr_closeout_impl.sh").read_text(encoding="utf-8")

    assert (
        'python3() { PYTHONPATH="$SCRIPT_DIR" "$PYTHON_HELPER_BIN" -P "$@"; }' in shell
    )
    assert "export PYTHONNOUSERSITE=1" in shell
    assert '"$BASH_BIN" "$SCRIPT_DIR/job.sh"' in shell
    assert '-- "$BASH_BIN" -c' in shell
    assert (
        'if [ "$DO_MERGE" -eq 1 ] && [ "${INTELFLO_BOOTSTRAP:-}" != "reviewed-main-v1" ]'
        in shell
    )


def test_canonical_primary_rejects_shallow_launcher_path_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "launcher.py"
    launcher.write_text("trusted\n", encoding="utf-8")
    monkeypatch.setattr(Path, "resolve", lambda _self: Path("/launcher.py"))

    with pytest.raises(module.BootstrapError, match="outside scripts/util"):
        module._canonical_primary(launcher)


def test_snapshot_uses_bootstrap_remote_default_ref_not_tracking_head() -> None:
    shell = (ROOT / "scripts/util/pr_closeout_impl.sh").read_text(encoding="utf-8")

    bootstrap_branch = shell.index(
        'if [ "${INTELFLO_BOOTSTRAP:-}" = "reviewed-main-v1" ]; then',
        shell.index("# 3. Post-merge Finding Ledger reconciliation"),
    )
    tracking_branch = shell.index("refs/remotes/origin/HEAD", bootstrap_branch)
    trusted_ref = shell.index(
        'remote_default_ref="$INTELFLO_REMOTE_DEFAULT_REF"', bootstrap_branch
    )
    else_branch = shell.index("else", trusted_ref)

    assert trusted_ref < else_branch < tracking_branch
    assert 'checkout -q --detach "$fresh_oid"' in shell


def test_delivery_binding_comes_from_primary_and_rejects_repointed_git_file(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    delivery = tmp_path / "delivery"
    _init_repo(primary)
    (primary / "tracked.txt").write_text("base\n", encoding="utf-8")
    _commit_all(primary, "base")
    _git(primary, "worktree", "add", "-q", "-b", "delivery", str(delivery))

    binding = module.verify_delivery_binding(primary, delivery)

    assert binding.delivery_root == delivery.resolve()
    assert binding.git_dir.parent == primary / ".git" / "worktrees"

    (delivery / ".git").write_text(
        f"gitdir: {tmp_path / 'attacker.git'}\n", encoding="utf-8"
    )
    with pytest.raises(module.BootstrapError, match="registered Git metadata"):
        module.verify_delivery_binding(primary, delivery)


def test_authority_cleanliness_rejects_unrelated_primary_changes(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    _init_repo(primary)
    util = primary / "scripts/util"
    util.mkdir(parents=True)
    (util / "pr_closeout.py").write_text("trusted\n", encoding="utf-8")
    (primary / "notes.txt").write_text("clean\n", encoding="utf-8")
    _commit_all(primary, "base")

    (primary / "notes.txt").write_text("unrelated\n", encoding="utf-8")
    with pytest.raises(module.BootstrapError, match="canonical primary is dirty"):
        module.ensure_authority_clean(primary)

    _git(primary, "checkout", "--", "notes.txt")
    (util / "pr_closeout.py").write_text("candidate\n", encoding="utf-8")
    with pytest.raises(module.BootstrapError, match="canonical primary is dirty"):
        module.ensure_authority_clean(primary)


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_authority_cleanliness_rejects_hidden_index_flags(
    tmp_path: Path, index_flag: str
) -> None:
    primary = tmp_path / "primary"
    _init_repo(primary)
    tracked = primary / "tracked.txt"
    tracked.write_text("reviewed\n", encoding="utf-8")
    _commit_all(primary, "base")
    _git(primary, "update-index", index_flag, "tracked.txt")
    tracked.write_text("hidden mutation\n", encoding="utf-8")

    with pytest.raises(module.BootstrapError, match="nonordinary index flags"):
        module.ensure_authority_clean(primary)


def test_launcher_parses_primary_repair_as_a_bootstrap_only_mode() -> None:
    options, forwarded = module.parse_launcher_arguments(["--repair-primary"])

    assert options.repair_primary is True
    assert forwarded == []

    with pytest.raises(module.BootstrapError, match="cannot be combined"):
        module.parse_launcher_arguments(["--repair-primary", "--pr", "123"])


def test_primary_repair_fetches_and_reparks_under_reviewed_toolchain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    primary = tmp_path / "primary"
    subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(remote)], check=True)
    _init_repo(seed)
    _git(seed, "branch", "-M", "main")
    util = seed / "scripts/util"
    util.mkdir(parents=True)
    (util / "worktree_guard.py").write_bytes(
        (ROOT / "scripts/util/worktree_guard.py").read_bytes()
    )
    (util / "pr_closeout_impl.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (util / "pr_closeout_trust_floor.json").write_text(
        '{"epoch": 1}\n', encoding="utf-8"
    )
    marker = seed / "marker.txt"
    marker.write_text("reviewed-one\n", encoding="utf-8")
    first = _commit_all(seed, "reviewed one")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    subprocess.run(
        ["/usr/bin/git", "clone", "-q", str(remote), str(primary)], check=True
    )
    _git(primary, "checkout", "-q", "--detach", first)

    marker.write_text("reviewed-two\n", encoding="utf-8")
    second = _commit_all(seed, "reviewed two")
    _git(seed, "push", "-q", "origin", "main")

    binding = module.RepositoryBinding(str(remote), "a" * 64)
    monkeypatch.setattr(module, "read_repository_binding", lambda *args: binding)

    module.repair_primary(
        primary,
        binding,
        "refs/heads/main",
        second,
        module.isolated_environment({}, home=tmp_path, user="operator"),
    )

    assert _git(primary, "rev-parse", "HEAD") == second
    assert _git(primary, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert (primary / "marker.txt").read_text(encoding="utf-8") == "reviewed-two\n"


def test_repository_binding_rejects_configured_url_rewrite(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _init_repo(primary)
    _git(primary, "remote", "add", "origin", "https://github.com/example/repo.git")
    _git(
        primary,
        "config",
        "url.https://attacker.invalid/.insteadOf",
        "https://github.com/",
    )

    with pytest.raises(module.BootstrapError, match="execution or redirection"):
        module.read_repository_binding(primary, module.isolated_environment(os.environ))


def test_repository_binding_rejects_core_worktree_redirection(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    redirected = tmp_path / "redirected"
    _init_repo(primary)
    redirected.mkdir()
    _git(primary, "remote", "add", "origin", "https://github.com/example/repo.git")
    _git(primary, "config", "core.worktree", str(redirected))

    with pytest.raises(module.BootstrapError, match="execution or redirection"):
        module.read_repository_binding(primary, module.isolated_environment(os.environ))


@pytest.mark.parametrize(
    "key",
    [
        "credential.helper",
        "http.proxy",
        "https.proxy",
        "include.path",
        "includeIf.gitdir:example.path",
        "url.https://example.invalid/.insteadOf",
        "protocol.file.allow",
        "diff.external.command",
        "filter.lfs.clean",
        "merge.custom.driver",
        "extensions.worktreeConfig",
        "core.askPass",
        "core.fsMonitor",
        "core.gitProxy",
        "core.hooksPath",
        "core.pager",
        "core.worktree",
        "core.sshCommand",
        "core.alternateRefsCommand",
        "alias.publish",
        "gpg.program",
        "remote.origin.pushUrl",
        "remote.origin.uploadPack",
        "remote.origin.url",
        "remote.origin.fetch",
        "branch.main.remote",
        "core.repositoryFormatVersion",
    ],
)
def test_bootstrap_config_redirection_policy_matches_shared_helper(key: str) -> None:
    assert module._git_config_key_can_redirect(
        key
    ) == git_config_security.git_config_key_can_redirect(key)


def test_bootstrap_config_redirection_policy_has_structural_parity() -> None:
    def body(function: object) -> str:
        source = textwrap.dedent(inspect.getsource(function))
        function_node = ast.parse(source).body[0]
        assert isinstance(function_node, ast.FunctionDef)
        return ast.dump(ast.Module(body=function_node.body, type_ignores=[]))

    assert body(module._git_config_key_can_redirect) == body(
        git_config_security.git_config_key_can_redirect
    )


def test_bootstrap_config_digest_matches_shared_helper(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _init_repo(primary)
    _git(primary, "remote", "add", "origin", "https://github.com/example/repo.git")
    _git(primary, "config", "branch.main.remote", "origin")
    config = primary / ".git/config"

    binding = module.read_repository_binding(
        primary, module.isolated_environment(os.environ)
    )

    assert binding.config_sha256 == git_config_security.security_projection_sha256(
        config.read_bytes()
    )


def test_remote_default_identity_uses_explicit_origin_not_local_tracking_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    remote_oid = "a" * 40
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def fake_run_git(
        repo: Path,
        *arguments: str,
        environment: dict[str, str] | None = None,
        **kwargs,
    ) -> subprocess.CompletedProcess[str]:
        assert repo == tmp_path
        assert environment is not None
        calls.append((arguments, environment))
        assert kwargs["timeout"] == 90
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout="ref: refs/heads/trunk\tHEAD\n" f"{remote_oid}\tHEAD\n",
            stderr="",
        )

    monkeypatch.setattr(module, "_run_git", fake_run_git)
    binding = module.RepositoryBinding("https://github.com/example/repo.git", "b" * 64)

    assert module.remote_default_identity(
        tmp_path, binding, module.isolated_environment(os.environ)
    ) == (
        "refs/heads/trunk",
        remote_oid,
    )
    assert len(calls) == 1
    arguments, environment = calls[0]
    assert arguments == (
        "ls-remote",
        "--symref",
        "--exit-code",
        binding.origin_url,
        "HEAD",
    )
    assert environment["GIT_CONFIG_COUNT"] == "5"


def test_materialized_toolchain_ignores_export_ignore_and_candidate_changes(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    delivery = tmp_path / "delivery"
    snapshot = tmp_path / "snapshot"
    _init_repo(primary)
    util = primary / "scripts/util"
    util.mkdir(parents=True)
    (util / "pr_closeout_impl.sh").write_text(
        '#!/bin/bash\nprintf trusted >"$TRUSTED_MARKER"\n', encoding="utf-8"
    )
    (util / "pr_closeout_impl.sh").chmod(0o755)
    (primary / ".gitattributes").write_text(
        "scripts/util/pr_closeout_impl.sh export-ignore\n", encoding="utf-8"
    )
    trusted_revision = _commit_all(primary, "trusted")
    _git(primary, "worktree", "add", "-q", "-b", "delivery", str(delivery))
    (delivery / "scripts/util/pr_closeout_impl.sh").write_text(
        '#!/bin/bash\nprintf attacker >"$ATTACKER_MARKER"\n', encoding="utf-8"
    )

    tree_oid = module.materialize_toolchain(primary, trusted_revision, snapshot)

    trusted_shell = snapshot / "scripts/util/pr_closeout_impl.sh"
    trusted_marker = tmp_path / "trusted-ran"
    attacker_marker = tmp_path / "attacker-ran"
    subprocess.run(
        ["/bin/bash", "-p", str(trusted_shell)],
        env={
            "PATH": "/usr/bin:/bin",
            "TRUSTED_MARKER": str(trusted_marker),
            "ATTACKER_MARKER": str(attacker_marker),
        },
        cwd=delivery,
        check=True,
    )

    assert tree_oid == _git(primary, "rev-parse", f"{trusted_revision}:scripts/util")
    assert trusted_marker.read_text(encoding="utf-8") == "trusted"
    assert not attacker_marker.exists()
    assert trusted_shell.stat().st_mode & 0o777 == 0o500
    assert snapshot.stat().st_mode & 0o777 == 0o700
    assert (snapshot / "scripts").stat().st_mode & 0o777 == 0o700
    assert (snapshot / "scripts" / "util").stat().st_mode & 0o777 == 0o700


def test_materialized_toolchain_rejects_symlink_entries(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    snapshot = tmp_path / "snapshot"
    _init_repo(primary)
    util = primary / "scripts/util"
    util.mkdir(parents=True)
    (util / "pr_closeout_impl.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (util / "outside-link").symlink_to("../../outside")
    revision = _commit_all(primary, "symlink")

    with pytest.raises(module.BootstrapError, match="regular blobs"):
        module.materialize_toolchain(primary, revision, snapshot)

    assert not snapshot.exists()


def test_rollback_requires_reason_ancestry_and_trust_floor(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _init_repo(primary)
    util = primary / "scripts/util"
    util.mkdir(parents=True)
    (util / "pr_closeout_impl.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    below_floor = _commit_all(primary, "before floor")
    (util / "pr_closeout_trust_floor.json").write_text(
        json.dumps({"epoch": 1}) + "\n", encoding="utf-8"
    )
    floor_revision = _commit_all(primary, "trust floor")
    (primary / "later.txt").write_text("later\n", encoding="utf-8")
    remote_revision = _commit_all(primary, "later")

    with pytest.raises(module.BootstrapError, match="rollback reason"):
        module.validate_rollback(
            primary, floor_revision, remote_revision, rollback_reason=""
        )
    with pytest.raises(module.BootstrapError, match="trust floor"):
        module.validate_rollback(
            primary, below_floor, remote_revision, rollback_reason="test rollback"
        )

    module.validate_rollback(
        primary,
        floor_revision,
        remote_revision,
        rollback_reason="test rollback",
    )


def test_rollback_rejects_second_parent_commit_not_on_reviewed_main_history(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    _init_repo(primary)
    util = primary / "scripts/util"
    util.mkdir(parents=True)
    (util / "pr_closeout_impl.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (util / "pr_closeout_trust_floor.json").write_text(
        json.dumps({"epoch": 1}) + "\n", encoding="utf-8"
    )
    _commit_all(primary, "floor")
    main_branch = _git(primary, "rev-parse", "--abbrev-ref", "HEAD")
    _git(primary, "checkout", "-qb", "candidate")
    (primary / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    candidate_revision = _commit_all(primary, "candidate")
    _git(primary, "checkout", "-q", main_branch)
    (primary / "main.txt").write_text("main\n", encoding="utf-8")
    _commit_all(primary, "main")
    _git(primary, "merge", "--no-ff", "-m", "merge candidate", "candidate")
    remote_revision = _git(primary, "rev-parse", "HEAD")

    with pytest.raises(module.BootstrapError, match="first-parent"):
        module.validate_rollback(
            primary,
            candidate_revision,
            remote_revision,
            rollback_reason="test rollback",
        )


def test_bootstrap_shell_uses_launcher_pinned_python_and_git() -> None:
    shell = (ROOT / "scripts/util/pr_closeout_impl.sh").read_text(encoding="utf-8")

    assert 'git() { "$GIT_BIN" "$@"; }' in shell
    assert (
        'python3() { PYTHONPATH="$SCRIPT_DIR" "$PYTHON_HELPER_BIN" -P "$@"; }' in shell
    )


def test_committed_trust_floor_matches_launcher_epoch() -> None:
    payload = json.loads(
        (ROOT / "scripts/util/pr_closeout_trust_floor.json").read_text(encoding="utf-8")
    )

    assert payload == {"epoch": module.TRUST_FLOOR_EPOCH}


@pytest.mark.parametrize(
    "mode", ["valid", "missing", "mismatched", "closed", "ordinary"]
)
def test_bootstrap_preserves_only_validated_lease_through_shell(
    tmp_path: Path, mode: str
) -> None:
    primary = tmp_path / "primary"
    delivery = tmp_path / "delivery"
    _init_repo(primary)
    _git(primary, "remote", "add", "origin", "https://github.com/example/repo.git")
    util = primary / "scripts/util"
    util.mkdir(parents=True)
    (util / "worktree_guard.py").write_bytes(
        (ROOT / "scripts/util/worktree_guard.py").read_bytes()
    )
    (util / "pr_closeout_trust_floor.json").write_text('{"epoch":1}')
    unrelated = os.open(tmp_path / "unrelated", os.O_CREAT | os.O_RDWR, 0o600)
    unrelated_high = fcntl.fcntl(unrelated, fcntl.F_DUPFD, 200)
    os.close(unrelated)
    probe = (
        "import os,sys; "
        f"fd={unrelated_high}; "
        "\ntry: os.fstat(fd)\nexcept OSError: print('LEASE_PROBE_OK')\nelse: sys.exit(77)\n"
    )
    shell = (
        """#!/bin/bash
set -eu
script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
exec /usr/bin/python3 -I "$script_dir/worktree_guard.py" exec --worktree "$INTELFLO_DELIVERY_ROOT" --boundary synthetic-lifecycle --timeout 0 -- /usr/bin/python3 -I -c """
        + shlex.quote(probe)
        + "\n"
    )
    (util / "pr_closeout_impl.sh").write_text(shell)
    revision = _commit_all(primary, "trusted synthetic lifecycle probe")
    _git(primary, "worktree", "add", "-q", "-b", "delivery", str(delivery))
    # A delegated checkout must never supply the imported guard implementation.
    (delivery / "scripts/util/worktree_guard.py").write_text(
        "raise RuntimeError('UNTRUSTED_GUARD_IMPORTED')\n"
    )
    git_dir = Path(_git(delivery, "rev-parse", "--absolute-git-dir"))
    lease_fd = os.open(
        git_dir / "worktree-boundary.lock", os.O_CREAT | os.O_RDWR, 0o600
    )
    if mode != "ordinary":
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    driver = tmp_path / "driver.py"
    driver.write_text(
        textwrap.dedent("""
        import importlib.util, os, sys
        from pathlib import Path
        source, primary, revision, mode = sys.argv[1:]
        spec = importlib.util.spec_from_file_location('synthetic_closeout', source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module._canonical_primary = lambda _path: Path(primary)
        module.remote_default_identity = lambda *_args: ('refs/heads/master', revision)
        if mode == 'closed':
            os.close(int(os.environ['INTELFLO_WORKTREE_LEASE_FD']))
        try:
            sys.exit(module._bootstrap(['--pr', '1', '--skill', 'work-issue']))
        except module.BootstrapError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(2)
    """)
    )
    environment = {"PATH": "/usr/bin:/bin"}
    inherited = [unrelated_high]
    if mode in {"valid", "closed"}:
        environment["INTELFLO_WORKTREE_LEASE_FD"] = str(lease_fd)
        inherited.append(lease_fd)
    elif mode == "mismatched":
        environment["INTELFLO_WORKTREE_LEASE_FD"] = str(unrelated_high)
    try:
        result = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                str(driver),
                str(MODULE_PATH),
                str(primary),
                revision,
                mode,
            ],
            cwd=delivery,
            env=environment,
            pass_fds=tuple(inherited),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if mode in {"valid", "ordinary"}:
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == "LEASE_PROBE_OK"
        else:
            assert result.returncode != 0
            assert "LEASE_PROBE_OK" not in result.stdout
            assert "lease" in result.stderr.lower()
        assert "UNTRUSTED_GUARD_IMPORTED" not in result.stderr
        # Child refusal/exit must not close the parent's unrelated descriptor.
        os.fstat(unrelated_high)
    finally:
        os.close(lease_fd)
        os.close(unrelated_high)
