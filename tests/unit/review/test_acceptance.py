"""Tests for the acceptance-command engine (dispatch_acceptance.py, #3944).

The engine is exercised through the agent_dispatch facade so re-exports stay
covered; monkeypatches go through _setattr_dispatch so every module-family
namespace that binds a name is patched (mock-hygiene: patch the reference
looked up at call time).
"""

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


from loopzero.kernel import trusted_exec as trusted_executable
from loopzero.review import _acceptance_grammar
from loopzero.review import acceptance as module

module.configure(
    SimpleNamespace(
        toolchain={
            "interpreter": "fastapi_backend/.venv/bin/python",
            "shared_artifacts": [
                "fastapi_backend/.venv", "nextjs-frontend/node_modules"
            ],
            "dotenv": "fastapi_backend/.env",
            "db_url_vars": ["TEST_DATABASE_URL", "DATABASE_URL"],
            "db_default_url": "postgresql://localhost:5433/test_db",
            "db_lock": "/tmp/intelflo-testdb-5433.lock",
            "db_targets": [
                "test", "test-be", "test-be-integration", "test-be-slow",
                "test-be-critical-integration", "test-be-coverage",
                "test-be-warnings", "test-be-profile", "test-be-precommit",
                "test-be-precommit-critical", "test-quota-gate-coverage",
            ],
        },
        audit_root=Path(".audit"),
        env_prefix="INTELFLO",
    )
)
_MOVED_DISPATCH_MODULES = (module, _acceptance_grammar)


def _setattr_dispatch(monkeypatch, name: str, value) -> None:
    """Patch a facade name in every module family namespace that binds it."""
    monkeypatch.setattr(module, name, value)
    for sibling in _MOVED_DISPATCH_MODULES:
        if hasattr(sibling, name):
            monkeypatch.setattr(sibling, name, value)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True
    )
    (tmp_path / "allowed.py").write_text("original\n")
    (tmp_path / ".gitignore").write_text("fastapi_backend/.venv/\n")
    (tmp_path / "fastapi_backend" / ".venv").mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "allowed.py", ".gitignore"],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "update-ref", "refs/remotes/origin/main", "HEAD"],
        check=True,
    )
    (tmp_path / ".branch-marker").write_text("review branch\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".branch-marker"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "branch baseline"],
        check=True,
    )
    return tmp_path


class TestDatabasePreflightAndTestScopeVisibility:
    """WF-2026-07-29-5/6/7: sandbox-without-DB and repurposed-test guards."""

    def test_db_dependent_acceptance_commands_are_recognized(self) -> None:
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest tests/e2e/test_smoke.py -q"
        )
        assert module.acceptance_requires_database(
            "TEST_ENV=1 pytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database("env TEST_ENV=1 make test-be")
        assert module.acceptance_requires_database(
            "uv run --group dev python -m pytest tests/e2e/test_smoke.py -q"
        )
        assert module.acceptance_requires_database(
            "uv run python -m pytest tests/e2e/test_smoke.py -q"
        )
        assert module.acceptance_requires_database(
            "env -u PYTHONPATH pytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            "env --unset PYTHONPATH pytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            "env --unset=PYTHONPATH pytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            "env --chdir fastapi_backend pytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            "env -- pytest tests/e2e/test_smoke.py -q"
        )
        assert module.acceptance_requires_database(
            "uv run --directory fastapi_backend python -m pytest "
            "tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            "uv run --python=3.13 pytest tests/e2e/test_smoke.py -q"
        )
        assert module.acceptance_requires_database("pytest tests")
        assert module.acceptance_requires_database("pytest tests/")
        assert module.acceptance_requires_database("pytest fastapi_backend/tests")
        assert module.acceptance_requires_database("uv run -q make test-be")
        assert module.acceptance_requires_database("uv run --with pkg pytest")
        assert module.acceptance_requires_database("uv run --with")
        assert module.acceptance_requires_database("uv run --mystery make test-be-unit")
        assert module.acceptance_requires_database(
            "true\npytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            "true;\npytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            "true # source-only setup\npytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            "echo '# literal' && pytest tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database(
            'bash -lc "pytest tests/integration/test_teams.py -q"'
        )
        assert module.acceptance_requires_database(
            "timeout 600 pytest tests/e2e/test_smoke.py -q"
        )
        assert module.acceptance_requires_database(
            "pytest 'tests/integration/test_teams.py -q"
        )
        assert module.acceptance_requires_database("make test-be")
        assert module.acceptance_requires_database("make test-be-integration")
        assert module.acceptance_requires_database("make test-be-coverage")
        assert module.acceptance_requires_database("make test-be-critical-integration")
        assert module.acceptance_requires_database("make test-be-precommit")
        assert module.acceptance_requires_database("make test-be-precommit-critical")
        assert module.acceptance_requires_database("make test-quota-gate-coverage")
        assert module.acceptance_requires_database("timeout 600 make test-be")
        assert module.acceptance_requires_database("timeout 1.5s make test-be")
        assert module.acceptance_requires_database("timeout -- make test-be")
        assert module.acceptance_requires_database("uv run make test-be")
        assert module.acceptance_requires_database(
            "uv run --directory fastapi_backend make test-be"
        )
        assert module.acceptance_requires_database('bash -lc "make test-be"')
        assert module.acceptance_requires_database('bash -lc "true;make test-be"')
        assert module.acceptance_requires_database('bash -lc "true&&make test-be"')
        assert module.acceptance_requires_database("make test")
        assert module.acceptance_requires_database('bash -lc "make test"')
        assert module.acceptance_requires_database('bash -o pipefail -c "make test-be"')
        assert module.acceptance_requires_database('bash -c -o pipefail "make test-be"')
        assert module.acceptance_requires_database('bash -co pipefail "make test-be"')
        assert module.acceptance_requires_database('bash -oc pipefail "make test-be"')
        assert module.acceptance_requires_database('bash -cO nocaseglob "make test-be"')
        assert module.acceptance_requires_database('bash -Oc nocaseglob "make test-be"')
        assert module.acceptance_requires_database('sh -co pipefail "make test-be"')
        assert module.acceptance_requires_database(
            'bash --rcfile /tmp/bashrc -c "make test-be"'
        )
        assert module.acceptance_requires_database('sh -o pipefail -c "make test-be"')
        assert module.acceptance_requires_database("bash -c")
        assert module.acceptance_requires_database("bash -o")
        assert module.acceptance_requires_database("bash --rcfile")
        assert module.acceptance_requires_database(
            'bash --init-file /tmp/bashrc -c "make test-be"'
        )
        assert module.acceptance_requires_database(
            'bash --init-file=/tmp/bashrc -c "make test-be"'
        )
        assert module.acceptance_requires_database('bash -O extglob -c "make test-be"')
        assert module.acceptance_requires_database(
            'bash -Onocaseglob -c "make test-be"'
        )
        assert module.acceptance_requires_database("bash -O")
        assert module.acceptance_requires_database("bash --init-file")
        assert module.acceptance_requires_database("pytest")
        assert module.acceptance_requires_database("pytest -q")
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -q"
        )
        assert module.acceptance_requires_database(
            'cd fastapi_backend && uv run pytest -q -m "critical and not slow"'
        )
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -q -k focused"
        )
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -q -k tests"
        )
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -q -n auto"
        )
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -q -p no:warnings"
        )
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -q -W default"
        )
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -q --rootdir ."
        )
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -q --rootdir tests"
        )
        assert module.acceptance_requires_database(
            'bash -lc "cd fastapi_backend && uv run pytest -q"'
        )
        assert module.acceptance_requires_database("timeout 600 pytest -q")
        assert module.acceptance_requires_database("pytest -vv")
        assert module.acceptance_requires_database("pytest --pdb")
        assert module.acceptance_requires_database("pytest --collect-only")
        assert module.acceptance_requires_database("pytest --disable-warnings")
        assert module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -vv --pdb"
        )
        assert module.acceptance_requires_database("pytest tests/unit --unknown-flag")
        assert module.acceptance_requires_database("timeout -vk 5s 600 make test-be")
        assert module.acceptance_requires_database("timeout -k 5s 600 make test-be")
        assert module.acceptance_requires_database("timeout -k5s 600 make test-be")
        assert module.acceptance_requires_database("timeout -s TERM 600 make test-be")
        assert module.acceptance_requires_database("timeout --kill 5s 600 make test-be")
        assert module.acceptance_requires_database(
            "timeout --sig TERM 600 make test-be"
        )
        assert module.acceptance_requires_database(
            "timeout --kill-after=5s 600 make test-be"
        )
        assert module.acceptance_requires_database("timeout -- 600 make test-be")
        assert module.acceptance_requires_database("timeout -vk")
        assert module.acceptance_requires_database("timeout -vk 5s")
        assert not module.acceptance_requires_database('bash -c ""')
        assert not module.acceptance_requires_database("make test-be-unit")
        assert not module.acceptance_requires_database("make test-be-precommit-unit")
        assert not module.acceptance_requires_database('bash -lc "make test-be-unit"')
        assert not module.acceptance_requires_database(
            'bash -c -o pipefail "make test-be-unit"'
        )
        assert not module.acceptance_requires_database("bash ./make-test-be.sh")
        assert not module.acceptance_requires_database("sh make-test-be.sh")
        assert not module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest tests/unit/test_teams.py -q"
        )
        assert not module.acceptance_requires_database("pytest tests/unit")
        assert not module.acceptance_requires_database("uv run -q make test-be-unit")
        assert not module.acceptance_requires_database(
            "uv run --with pkg pytest tests/unit -q"
        )
        assert not module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest "
            "tests/unit/scripts/test_agent_dispatch.py -q"
        )
        assert not module.acceptance_requires_database(
            'cd fastapi_backend && uv run pytest tests/unit -m "not slow" -q'
        )
        assert not module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -q -k tests tests/unit"
        )
        assert not module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest -x tests/unit -q"
        )
        assert not module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest "
            "tests/unit/test_file.py::test_name -vv"
        )
        assert not module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest tests/unit -vv --pdb"
        )
        assert not module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest tests/unit --collect-only"
        )
        assert not module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest tests/unit --disable-warnings -q"
        )
        assert not module.acceptance_requires_database(
            "cd fastapi_backend && uv run pytest tests/unit "
            "--strict-markers --no-header -qvx"
        )
        assert not module.acceptance_requires_database(
            'bash -lc "cd fastapi_backend && uv run pytest tests/unit -q"'
        )
        assert not module.acceptance_requires_database(
            "pytest --junitxml tests/integration/results.xml tests/unit"
        )
        assert not module.acceptance_requires_database(
            "pytest --junitxml=tests/integration/results.xml tests/unit"
        )
        assert not module.acceptance_requires_database(
            "timeout -vk 5s 600 make test-be-unit"
        )
        assert not module.acceptance_requires_database(
            "timeout -k5s 600 make test-be-unit"
        )
        assert not module.acceptance_requires_database(
            "timeout --kill 5s 600 make test-be-unit"
        )
        assert not module.acceptance_requires_database(
            "timeout --sig TERM 600 make test-be-unit"
        )
        assert not module.acceptance_requires_database(
            "timeout --kill-after=5s 600 make test-be-unit"
        )
        assert module.acceptance_requires_database(
            "timeout --ver 600 make test-be-unit"
        )
        assert module.acceptance_requires_database("timeout -z 600 make test-be-unit")
        assert not module.acceptance_requires_database("rg 'make test-be'")
        assert not module.acceptance_requires_database("rg make test-be")
        assert not module.acceptance_requires_database("ruff check tests/integration")
        assert not module.acceptance_requires_database("python -m ruff check tests/e2e")
        assert not module.acceptance_requires_database(
            "echo pretend && true # tests/integration/"
        )
        assert not module.acceptance_requires_database(
            "TEST_ENV=1 ruff check tests/integration"
        )
        assert not module.acceptance_requires_database(
            "env TEST_ENV=1 python -m ruff check tests/e2e"
        )
        assert not module.acceptance_requires_database(
            "env -u PYTHONPATH ruff check tests/integration"
        )
        assert not module.acceptance_requires_database(
            "env --unset PYTHONPATH ruff check tests/integration"
        )
        assert not module.acceptance_requires_database(
            "uv run ruff check tests/integration"
        )

    def test_preflight_fails_closed_when_test_db_is_unreachable(
        self, monkeypatch, tmp_path
    ) -> None:
        _setattr_dispatch(
            monkeypatch,
            "test_database_unreachable",
            lambda worktree: "test database unreachable for DB-dependent acceptance",
        )

        errors = module.preflight_acceptance_tooling(
            ["cd fastapi_backend && uv run pytest tests/integration -q"],
            worktree=tmp_path,
        )

        assert any("test database unreachable" in error for error in errors)

    def test_database_probe_uses_only_captured_dispatcher_authority(
        self, monkeypatch, tmp_path
    ) -> None:
        primary = tmp_path / "primary"
        backend = primary / "fastapi_backend"
        backend.mkdir(parents=True)
        (backend / ".env").write_text(
            "TEST_DATABASE_URL=postgresql://db.example:5544/test_db\n",
            encoding="utf-8",
        )
        observed: list[tuple[tuple[str, int], float]] = []

        class Connection:
            def close(self) -> None:
                return None

        def connect(address, *, timeout):
            observed.append((address, timeout))
            return Connection()

        import socket

        monkeypatch.setattr(socket, "create_connection", connect)
        authority = module.capture_acceptance_authority(primary, db_bound=True)

        assert module.test_database_unreachable(authority) is None
        assert observed == [(("db.example", 5544), 1.0)]

    def test_primary_repository_resolution_ignores_inherited_git_overrides(
        self, monkeypatch, tmp_path
    ) -> None:
        observed: dict[str, object] = {}

        def run(argv, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(
                returncode=0,
                stdout=str(tmp_path / "primary" / ".git") + "\n",
                stderr="",
            )

        monkeypatch.setenv("GIT_DIR", str(tmp_path / "attacker" / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "attacker"))
        monkeypatch.setattr(module.subprocess, "run", run)

        assert module.primary_repo_root(tmp_path) == tmp_path / "primary"
        environment = observed["env"]
        assert isinstance(environment, dict)
        assert "GIT_DIR" not in environment
        assert "GIT_WORK_TREE" not in environment

    def test_preflight_skips_db_probe_for_unit_only_acceptance(
        self, monkeypatch, tmp_path
    ) -> None:
        def _unexpected_probe(worktree: Path) -> str | None:
            raise AssertionError("DB probe must not run for unit-only acceptance")

        _setattr_dispatch(monkeypatch, "test_database_unreachable", _unexpected_probe)
        monkeypatch.setattr(module.shutil, "which", lambda executable: "/usr/bin/tool")
        pytest_binary = tmp_path / "fastapi_backend" / ".venv" / "bin" / "pytest"
        pytest_binary.parent.mkdir(parents=True)
        pytest_binary.write_text("#!/bin/sh\n")
        pytest_binary.chmod(0o755)

        errors = module.preflight_acceptance_tooling(
            ["cd fastapi_backend && uv run pytest tests/unit -q"],
            worktree=tmp_path,
        )

        assert errors == []

    def test_db_unreachable_output_is_shortlisted(self) -> None:
        assert module._database_unreachable_output(
            "❌ test db not ready at localhost:5433: refused"
        )
        assert module._database_unreachable_output(
            "asyncpg.exceptions._base.clientcannotconnecterror: "
            "[errno 111] connection refused"
        )

    def test_refusal_without_database_marker_is_not_shortlisted(self) -> None:
        assert not module._database_unreachable_output(
            "failed test_client_retry.py - expected 'connection refused' banner"
        )
        assert not module._database_unreachable_output(
            "assertionerror: postgres row count 2 != 3"
        )

    def test_db_output_classifies_environment_only_with_live_confirmation(
        self, monkeypatch, tmp_path
    ) -> None:
        (tmp_path / "fastapi_backend" / ".venv").mkdir(parents=True)
        _setattr_dispatch(
            monkeypatch, "sandbox_command", lambda argv, **_kwargs: list(argv)
        )
        command = 'echo "Test DB not ready at localhost:5433" && exit 1'
        _setattr_dispatch(
            monkeypatch, "test_database_unreachable", lambda worktree: "db unreachable"
        )
        confirmed = module.run_acceptance_commands(
            [command], worktree=tmp_path, timeout_s=30
        )
        assert confirmed[0]["failure_class"] == "acceptance-environment"

        _setattr_dispatch(
            monkeypatch, "test_database_unreachable", lambda worktree: None
        )
        unconfirmed = module.run_acceptance_commands(
            [command], worktree=tmp_path, timeout_s=30
        )
        assert "failure_class" not in unconfirmed[0]

    def test_acceptance_uses_credential_free_sandbox_without_parent_fds(
        self, monkeypatch, tmp_path
    ) -> None:
        (tmp_path / "fastapi_backend" / ".venv").mkdir(parents=True)
        observed: dict[str, object] = {}

        def run(argv, **kwargs):
            observed["argv"] = argv
            observed.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="accepted\n", stderr="")

        monkeypatch.setenv("GH_TOKEN", "parent-authority")
        monkeypatch.setenv("GITHUB_TOKEN", "parent-authority")
        monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://test-only")
        monkeypatch.setenv("INTELFLO_WORKTREE_LEASE_BOUNDARY", "dispatch-write")
        monkeypatch.setenv("INTELFLO_WORKTREE_LEASE_FD", "41")
        monkeypatch.setenv("INTELFLO_WORKTREE_LEASE_NONCE", "parent-nonce")
        monkeypatch.setenv("INTELFLO_WORKTREE_LEASE_OWNER_PID", "1234")
        monkeypatch.setattr(module.subprocess, "run", run)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: False
        )
        _setattr_dispatch(
            monkeypatch,
            "primary_repo_root",
            lambda _worktree: (_ for _ in ()).throw(module.DispatchError("fixture")),
        )

        result = module._run_one_acceptance_command(
            "echo accepted", worktree=tmp_path, timeout_s=30
        )

        argv = observed["argv"]
        assert isinstance(argv, list)
        assert Path(argv[0]).is_absolute()
        assert Path(argv[0]).name == "bwrap"
        assert "--unshare-net" in argv
        environment = observed["env"]
        assert isinstance(environment, dict)
        assert "GH_TOKEN" not in environment
        assert "GITHUB_TOKEN" not in environment
        assert "TEST_DATABASE_URL" not in environment
        assert "DATABASE_URL" not in environment
        assert (
            not {
                "INTELFLO_WORKTREE_LEASE_BOUNDARY",
                "INTELFLO_WORKTREE_LEASE_FD",
                "INTELFLO_WORKTREE_LEASE_NONCE",
                "INTELFLO_WORKTREE_LEASE_OWNER_PID",
            }
            & environment.keys()
        )
        assert observed["close_fds"] is True
        assert "pass_fds" not in observed
        assert result["exit_code"] == 0

    def test_db_acceptance_reuses_sandbox_with_host_network(
        self, monkeypatch, tmp_path
    ) -> None:
        (tmp_path / "fastapi_backend" / ".venv").mkdir(parents=True)
        observed: dict[str, object] = {}

        def run(argv, **kwargs):
            observed["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(module.subprocess, "run", run)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: False
        )
        _setattr_dispatch(
            monkeypatch,
            "primary_repo_root",
            lambda _worktree: (_ for _ in ()).throw(module.DispatchError("fixture")),
        )

        module._run_one_acceptance_command(
            "echo db",
            worktree=tmp_path,
            timeout_s=30,
            db_bound=True,
            db_network_grant=True,
        )

        argv = observed["argv"]
        assert isinstance(argv, list)
        assert "--unshare-net" not in argv

        # Containment fails closed: db_bound alone (for example unparseable
        # grammar serialized as DB) never earns the network grant without the
        # caller's explicit db_commands marking or recognized DB grammar.
        module._run_one_acceptance_command(
            "echo db", worktree=tmp_path, timeout_s=30, db_bound=True
        )
        argv = observed["argv"]
        assert isinstance(argv, list)
        assert "--unshare-net" in argv

        # Positively recognized DB grammar earns the grant without explicit
        # marking (derived in _run_one when the caller does not thread it).
        module._run_one_acceptance_command(
            "make test-be",
            worktree=tmp_path,
            timeout_s=30,
            db_bound=True,
        )
        argv = observed["argv"]
        assert isinstance(argv, list)
        assert "--unshare-net" not in argv

    def test_db_acceptance_exposes_only_the_test_database_configuration(
        self, monkeypatch, tmp_path
    ) -> None:
        primary = tmp_path / "primary"
        worktree = tmp_path / "snapshot"
        backend = primary / "fastapi_backend"
        backend.mkdir(parents=True)
        (backend / ".venv").mkdir()
        worktree.mkdir()
        (backend / ".env").write_text(
            "DATABASE_URL=postgresql://production\n"
            "TEST_DATABASE_URL=postgresql://stale-test-db\n"
            "TEST_DATABASE_URL=postgresql://test-db # current test database\n"
            "UNRELATED_SECRET=do-not-forward\n",
            encoding="utf-8",
        )
        observed: dict[str, object] = {}

        def run(argv, **kwargs):
            del argv
            observed.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setenv("GH_TOKEN", "parent-authority")
        monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://inherited-override")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(module.subprocess, "run", run)
        _setattr_dispatch(monkeypatch, "primary_repo_root", lambda _worktree: primary)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: False
        )

        module._run_one_acceptance_command(
            "echo db", worktree=worktree, timeout_s=30, db_bound=True
        )

        environment = observed["env"]
        assert isinstance(environment, dict)
        assert environment["TEST_DATABASE_URL"] == "postgresql://test-db"
        assert environment["DATABASE_URL"] == "postgresql://test-db"
        assert environment["INTELFLO_DISABLE_DOTENV"] == "1"
        assert "UNRELATED_SECRET" not in environment
        assert "GH_TOKEN" not in environment

    def test_nested_acceptance_reuses_matching_guardian_boundary(
        self, monkeypatch, tmp_path
    ) -> None:
        (tmp_path / "fastapi_backend" / ".venv").mkdir(parents=True)
        observed: dict[str, object] = {}

        def run(argv, **kwargs):
            observed["argv"] = argv
            observed.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="accepted\n", stderr="")

        monkeypatch.setattr(module.subprocess, "run", run)
        _setattr_dispatch(monkeypatch, "primary_repo_root", lambda _worktree: tmp_path)
        _setattr_dispatch(
            monkeypatch,
            "current_boundary_reusable",
            lambda *, deny_network: deny_network,
        )
        _setattr_dispatch(
            monkeypatch, "system_executable", lambda name: Path(f"/usr/bin/{name}")
        )

        result = module._run_one_acceptance_command(
            "echo accepted", worktree=tmp_path, timeout_s=30
        )

        argv = observed["argv"]
        assert argv == ["/usr/bin/bash", "-c", "echo accepted"]
        assert observed["close_fds"] is True
        assert result["exit_code"] == 0

    def test_nested_acceptance_rejects_an_unproven_toolchain_overlay(
        self, monkeypatch, tmp_path
    ) -> None:
        primary = tmp_path / "primary"
        worktree = tmp_path / "worktree"
        (primary / "fastapi_backend" / ".venv").mkdir(parents=True)
        (worktree / "fastapi_backend" / ".venv").mkdir(parents=True)
        _setattr_dispatch(monkeypatch, "primary_repo_root", lambda _worktree: primary)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: True
        )
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail(
                "an unverified reusable boundary must not execute acceptance"
            ),
        )

        result = module._run_one_acceptance_command(
            "true", worktree=worktree, timeout_s=30
        )

        assert result["exit_code"] == 125
        assert result["failure_class"] == "acceptance-environment"
        assert "trusted toolchain mounts" in result["tail"]

    def test_snapshot_acceptance_mounts_primary_venv_when_destination_is_absent(
        self, monkeypatch, tmp_path
    ) -> None:
        primary = tmp_path / "primary"
        worktree = tmp_path / "snapshot"
        primary_venv = primary / "fastapi_backend" / ".venv"
        primary_venv.mkdir(parents=True)
        worktree.mkdir()
        observed: dict[str, object] = {}

        def build(argv, **kwargs):
            observed["argv"] = argv
            observed.update(kwargs)
            return ["/usr/bin/true"]

        _setattr_dispatch(monkeypatch, "primary_repo_root", lambda _worktree: primary)
        _setattr_dispatch(monkeypatch, "sandbox_command", build)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: False
        )

        result = module._run_one_acceptance_command(
            "true", worktree=worktree, timeout_s=30
        )

        assert result["exit_code"] == 0
        assert observed["read_only_mounts"] == (
            (primary_venv, worktree / "fastapi_backend" / ".venv"),
        )
        assert observed["read_only_roots"] == (
            primary_venv.resolve(),
            Path("/etc/alternatives"),
        )

    def test_acceptance_overlays_an_existing_worktree_venv_with_primary(
        self, monkeypatch, tmp_path
    ) -> None:
        primary = tmp_path / "primary"
        worktree = tmp_path / "worktree"
        primary_venv = primary / "fastapi_backend" / ".venv"
        worktree_venv = worktree / "fastapi_backend" / ".venv"
        primary_venv.mkdir(parents=True)
        worktree_venv.mkdir(parents=True)
        observed: dict[str, object] = {}

        def build(argv, **kwargs):
            observed.update(kwargs)
            return ["/usr/bin/true"]

        _setattr_dispatch(monkeypatch, "primary_repo_root", lambda _worktree: primary)
        _setattr_dispatch(monkeypatch, "sandbox_command", build)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: False
        )

        result = module._run_one_acceptance_command(
            "true", worktree=worktree, timeout_s=30
        )

        assert result["exit_code"] == 0
        assert observed["read_only_mounts"] == ((primary_venv, worktree_venv),)
        assert observed["read_only_roots"] == (
            primary_venv.resolve(),
            Path("/etc/alternatives"),
        )

    def test_acceptance_overlays_primary_frontend_dependencies(
        self, monkeypatch, tmp_path
    ) -> None:
        primary = tmp_path / "primary"
        worktree = tmp_path / "worktree"
        primary_venv = primary / "fastapi_backend" / ".venv"
        worktree_venv = worktree / "fastapi_backend" / ".venv"
        primary_modules = primary / "nextjs-frontend" / "node_modules"
        worktree_modules = worktree / "nextjs-frontend" / "node_modules"
        primary_venv.mkdir(parents=True)
        worktree_venv.mkdir(parents=True)
        primary_modules.mkdir(parents=True)
        worktree_modules.mkdir(parents=True)
        observed: dict[str, object] = {}

        def build(argv, **kwargs):
            observed["argv"] = argv
            observed.update(kwargs)
            return ["/usr/bin/true"]

        _setattr_dispatch(monkeypatch, "primary_repo_root", lambda _worktree: primary)
        _setattr_dispatch(monkeypatch, "sandbox_command", build)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: False
        )

        result = module._run_one_acceptance_command(
            "corepack pnpm -C nextjs-frontend test",
            worktree=worktree,
            timeout_s=30,
        )

        assert result["exit_code"] == 0
        assert observed["read_only_mounts"] == (
            (primary_venv, worktree_venv),
            (primary_modules, worktree_modules),
        )
        assert observed["read_only_roots"] == (
            primary_venv.resolve(),
            primary_modules.resolve(),
            Path("/etc/alternatives"),
        )
        assert observed["argv"][-1] == "corepack pnpm -C nextjs-frontend test"
        assert observed["include_model_runtime"] is False
        assert observed["include_corepack_runtime"] is True

    def test_nested_acceptance_fails_closed_without_corepack_runtime(
        self, monkeypatch, tmp_path
    ) -> None:
        primary = tmp_path / "primary"
        worktree = tmp_path / "worktree"
        primary_venv = primary / "fastapi_backend" / ".venv"
        primary_venv.mkdir(parents=True)
        (worktree / "fastapi_backend").mkdir(parents=True)
        (worktree / "fastapi_backend" / ".venv").symlink_to(
            primary_venv, target_is_directory=True
        )
        _setattr_dispatch(monkeypatch, "primary_repo_root", lambda _worktree: primary)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: True
        )
        monkeypatch.setattr(module.shutil, "which", lambda _name: None)
        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *args, **kwargs: pytest.fail(
                "a Corepack-less reusable boundary must not execute acceptance"
            ),
        )

        result = module._run_one_acceptance_command(
            "corepack pnpm -C nextjs-frontend test",
            worktree=worktree,
            timeout_s=30,
        )

        assert result["exit_code"] == 125
        assert result["failure_class"] == "acceptance-environment"
        assert "lacks Corepack runtime" in result["tail"]

    def test_acceptance_reuses_a_primary_frontend_dependency_symlink(
        self, monkeypatch, tmp_path
    ) -> None:
        primary = tmp_path / "primary"
        worktree = tmp_path / "worktree"
        primary_venv = primary / "fastapi_backend" / ".venv"
        worktree_venv = worktree / "fastapi_backend" / ".venv"
        primary_modules = primary / "nextjs-frontend" / "node_modules"
        worktree_modules = worktree / "nextjs-frontend" / "node_modules"
        primary_venv.mkdir(parents=True)
        worktree_venv.mkdir(parents=True)
        primary_modules.mkdir(parents=True)
        worktree_modules.parent.mkdir(parents=True)
        worktree_modules.symlink_to(primary_modules, target_is_directory=True)
        observed: dict[str, object] = {}

        def build(argv, **kwargs):
            observed.update(kwargs)
            return ["/usr/bin/true"]

        _setattr_dispatch(monkeypatch, "primary_repo_root", lambda _worktree: primary)
        _setattr_dispatch(monkeypatch, "sandbox_command", build)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: False
        )

        result = module._run_one_acceptance_command(
            "corepack pnpm -C nextjs-frontend test",
            worktree=worktree,
            timeout_s=30,
        )

        assert result["exit_code"] == 0
        assert observed["read_only_mounts"] == ((primary_venv, worktree_venv),)
        assert observed["read_only_roots"] == (
            primary_venv.resolve(),
            primary_modules.resolve(),
            Path("/etc/alternatives"),
        )

    def test_acceptance_fails_closed_without_the_primary_toolchain(
        self, monkeypatch, tmp_path
    ) -> None:
        primary = tmp_path / "primary"
        worktree = tmp_path / "worktree"
        primary.mkdir()
        (worktree / "fastapi_backend" / ".venv" / "bin").mkdir(parents=True)
        _setattr_dispatch(monkeypatch, "primary_repo_root", lambda _worktree: primary)
        _setattr_dispatch(
            monkeypatch,
            "sandbox_command",
            lambda *args, **kwargs: pytest.fail(
                "acceptance must stop before constructing a candidate-toolchain sandbox"
            ),
        )

        result = module._run_one_acceptance_command(
            "uv run pytest", worktree=worktree, timeout_s=30
        )

        assert result["exit_code"] == 125
        assert result["failure_class"] == "acceptance-environment"
        assert "trusted primary acceptance toolchain is unavailable" in result["tail"]

    @pytest.mark.parametrize("failure_source", ["executable", "sandbox"])
    def test_acceptance_sandbox_setup_failure_is_environmental(
        self, monkeypatch, tmp_path, failure_source
    ) -> None:
        (tmp_path / "fastapi_backend" / ".venv").mkdir(parents=True)
        _setattr_dispatch(
            monkeypatch, "current_boundary_reusable", lambda *, deny_network: False
        )
        if failure_source == "executable":
            _setattr_dispatch(
                monkeypatch,
                "system_executable",
                lambda name: (_ for _ in ()).throw(
                    module.TrustedExecutableError(f"unsafe {name}")
                ),
            )
        else:
            _setattr_dispatch(monkeypatch, "system_executable", lambda name: Path(name))
            _setattr_dispatch(
                monkeypatch,
                "sandbox_command",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    module.SandboxError("TOKEN=live-secret unavailable")
                ),
            )

        result = module._run_one_acceptance_command(
            "true", worktree=tmp_path, timeout_s=30
        )

        assert result["exit_code"] == 125
        assert result["failure_class"] == "acceptance-environment"
        assert "live-secret" not in result["tail"]


def test_explicit_db_commands_receive_network_grant_end_to_end(
    monkeypatch, tmp_path
) -> None:
    """A db_commands entry must reach execution with the network grant even
    when its grammar is unrecognized (review S-4 coverage gap)."""
    (tmp_path / "fastapi_backend" / ".venv").mkdir(parents=True)
    observed: list[tuple[str, bool | None]] = []

    def run_one(command, *, worktree, timeout_s, db_bound, db_network_grant, authority):
        observed.append((command, db_network_grant))
        return {"command": command, "exit_code": 0, "tail": ""}

    monkeypatch.setattr(module, "_run_one_acceptance_command", run_one)
    _setattr_dispatch(monkeypatch, "test_database_unreachable", lambda authority: None)
    _setattr_dispatch(
        monkeypatch,
        "_acquire_test_db_lock",
        lambda worktree, timeout_s: open(os.devnull),
    )

    module.run_acceptance_commands(
        ["echo plain"],
        worktree=tmp_path,
        timeout_s=30,
        db_commands=["custom-unrecognized-db-lane --weird"],
    )

    grants = dict(observed)
    assert grants["echo plain"] is False
    assert grants["custom-unrecognized-db-lane --weird"] is True


def test_preflight_reports_worktree_escaping_directory_operand(tmp_path) -> None:
    errors = module.preflight_acceptance_tooling(
        ["cd /etc && corepack pnpm test"], worktree=tmp_path
    )
    assert any("escapes the worktree" in error for error in errors)


def test_loose_token_db_classification_never_grants_network() -> None:
    """A path-looking token serializes as DB-bound but must not open the
    sandbox network (strict anchored-grammar grant only)."""
    command = "echo tests/integration/x && true"
    assert module.acceptance_requires_database(command) is True
    assert module._acceptance_network_grant(command, explicit_db=False) is False
    # Unparseable grammar: serialized as DB-bound, still no grant.
    assert (
        module._acceptance_network_grant(
            "uv run --mystery make test-be-unit", explicit_db=False
        )
        is False
    )
    # Anchored grammar keeps the grant, including standard no-value uv flags.
    assert (
        module._acceptance_network_grant(
            "cd fastapi_backend && uv run pytest tests/integration/test_x.py -q",
            explicit_db=False,
        )
        is True
    )
    assert (
        module._acceptance_network_grant(
            "cd fastapi_backend && uv run --frozen pytest tests/integration -q",
            explicit_db=False,
        )
        is True
    )
    assert (
        module._acceptance_network_grant(
            "cd fastapi_backend && uv run --all-groups pytest tests/integration -q",
            explicit_db=False,
        )
        is True
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "flock -w 30 /tmp/intelflo-testdb-5433.lock "
            "uv run pytest tests/integration/test_x.py -q",
            True,
        ),
        (
            "/usr/bin/flock -w 1 /tmp/intelflo-testdb-5433.lock "
            "make test-be-integration",
            True,
        ),
        (
            "bash -c 'flock -w 1800 /tmp/intelflo-testdb-5433.lock " "make test-be'",
            True,
        ),
        (
            "flock -w 30 /tmp/intelflo-testdb-5433.lock "
            "uv run pytest tests/unit/test_x.py -q",
            False,
        ),
        (
            "flock -w 30 /tmp/intelflo-testdb-5433.lock "
            "echo tests/integration/test_x.py",
            False,
        ),
        (
            "flock -w 0 /tmp/intelflo-testdb-5433.lock make test-be",
            False,
        ),
        (
            "flock -w 1801 /tmp/intelflo-testdb-5433.lock make test-be",
            False,
        ),
        ("flock -w 30 /tmp/intelflo-testdb-5433.lock", False),
        (
            "flock -x /tmp/intelflo-testdb-5433.lock make test-be",
            False,
        ),
        (
            "flock -w 30 /tmp/intelflo-testdb-5433.lock -c 'make test-be'",
            False,
        ),
        (
            "flock -w 30 /tmp/other.lock make test-be",
            False,
        ),
        (
            "flock -w 30 9 make test-be",
            False,
        ),
        (
            "bash -c 'flock -w 30 9 make test-be'",
            False,
        ),
        (
            "bash -c 'echo tests/integration/x && true'",
            False,
        ),
        (
            "flock -w 30 /tmp/intelflo-testdb-5433.lock "
            "uv run --mystery pytest tests/integration/test_x.py -q",
            False,
        ),
    ],
)
def test_canonical_flock_wrappers_control_network_grant(
    command: str, expected: bool
) -> None:
    assert module._acceptance_network_grant(command, explicit_db=False) is expected


@pytest.mark.parametrize(
    "command",
    [
        "flock -w 30 /tmp/intelflo-testdb-5433.lock ''",
        "flock -w 30 /tmp/intelflo-testdb-5433.lock -c 'make test-be'",
        "flock -w 30 /tmp/intelflo-testdb-5433.lock --command 'make test-be'",
        "flock -w 30 /tmp/intelflo-testdb-5433.lock --unknown 'make test-be'",
    ],
)
def test_unsupported_canonical_flock_prefixes_are_conservative(
    command: str,
) -> None:
    assert module.acceptance_requires_database(command) is True
    assert module._acceptance_network_grant(command, explicit_db=False) is False


def test_lock_timeout_result_redacts_persisted_command(monkeypatch, tmp_path) -> None:
    """The lock-timeout branch bypasses _run_one_acceptance_command, so it
    must redact the persisted command itself."""
    (tmp_path / "fastapi_backend" / ".venv").mkdir(parents=True)
    _setattr_dispatch(
        monkeypatch, "_acquire_test_db_lock", lambda worktree, timeout_s: None
    )
    results = module.run_acceptance_commands(
        [],
        worktree=tmp_path,
        timeout_s=30,
        db_commands=["make test-be TOKEN=live-secret-value"],
    )
    assert results[0]["exit_code"] == 124
    assert "live-secret-value" not in str(results[0]["command"])


@pytest.mark.parametrize(
    "kind", ["directory", "symlink", "hardlink", "traversal", "glob"]
)
def test_exact_repair_file_binding_refuses_aliases(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "target.py"
    target.write_text("original")
    path = "target.py"
    if kind == "directory":
        path = "."
    elif kind == "symlink":
        (tmp_path / "alias.py").symlink_to(target)
        path = "alias.py"
    elif kind == "hardlink":
        os.link(target, tmp_path / "alias.py")
    elif kind == "traversal":
        path = "../target.py"
    else:
        path = "*.py"
    with pytest.raises(module.SandboxError):
        module.exact_repair_file_bindings(tmp_path, (path,))


def _track_repair_fixture(tmp_path: Path, *, ignored_target: bool = False) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(
        ".audit/\nfastapi_backend/\nnextjs-frontend/\nsibling.py\n"
        + ("target.py\n" if ignored_target else "")
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f", ".gitignore", "target.py"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "Synthetic repair fixture",
        ],
        check=True,
    )


def test_exact_repair_tracked_membership_is_literal(tmp_path: Path) -> None:
    (tmp_path / "target.py").write_text("tracked")
    _track_repair_fixture(tmp_path)
    (tmp_path / "literal[1].py").write_text("tracked literal")
    (tmp_path / "literal1.py").write_text("untracked sibling")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "--literal-pathspecs",
            "add",
            "--",
            "literal[1].py",
        ],
        check=True,
    )
    assert module.exact_repair_file_bindings(tmp_path, ("literal[1].py",)) == [
        "--bind",
        str(tmp_path / "literal[1].py"),
        str(tmp_path / "literal[1].py"),
    ]
    with pytest.raises(module.SandboxError, match="tracked source"):
        module.exact_repair_file_bindings(tmp_path, ("literal1.py",))


def test_exact_repair_tracked_lookup_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "target.py").write_text("synthetic")
    with pytest.raises(module.SandboxError, match="cannot verify tracked source"):
        module.exact_repair_file_bindings(tmp_path, ("target.py",))


@pytest.mark.parametrize("ignored", [False, True])
def test_exact_repair_rejects_untracked_bind_consumers(
    tmp_path: Path, ignored: bool
) -> None:
    acceptance = module

    primary = module.primary_repo_root(Path(__file__).resolve().parents[3])
    for relative in ("fastapi_backend/.venv", "nextjs-frontend/node_modules"):
        destination = tmp_path / relative
        destination.parent.mkdir()
        destination.symlink_to(primary / relative, target_is_directory=True)
    (tmp_path / "target.py").write_text("tracked fixture")
    _track_repair_fixture(tmp_path)
    target = tmp_path / "untracked.py"
    target.write_text("synthetic original")
    if ignored:
        with (tmp_path / ".gitignore").open("a") as handle:
            handle.write("untracked.py\n")
    result = acceptance._run_one_acceptance_command(
        "/usr/bin/python3 -c \"from pathlib import Path; Path('untracked.py').write_text('changed')\"",
        worktree=tmp_path,
        timeout_s=30,
        authority=acceptance.AcceptanceAuthority(primary=primary, test_database_url=None),
        source_write_files=("untracked.py",),
    )
    assert result["exit_code"] != 0
    assert "tracked source" in str(result)
    assert target.read_text() == "synthetic original"


@pytest.mark.parametrize("ignored_target", [False, True])
def test_exact_repair_prevents_unlisted_writes(
    tmp_path: Path, ignored_target: bool
) -> None:
    import shlex

    acceptance = module

    primary = module.primary_repo_root(Path(__file__).resolve().parents[3])
    for relative in ("fastapi_backend/.venv", "nextjs-frontend/node_modules"):
        destination = tmp_path / relative
        destination.parent.mkdir()
        destination.symlink_to(primary / relative, target_is_directory=True)
    (tmp_path / "target.py").write_text("original")
    (tmp_path / "sibling.py").write_text("untouched")
    _track_repair_fixture(tmp_path, ignored_target=ignored_target)
    code = """from pathlib import Path
p = Path('target.py'); p.write_text('allowed')
for q in [Path('sibling.py'), Path('new-sibling.py')]:
    try: q.write_text('escape')
    except OSError: pass
    else: raise AssertionError('unlisted write permitted: '+str(q))
try: p.unlink()
except OSError: pass
else: raise AssertionError('source replacement permitted')
"""
    result = acceptance._run_one_acceptance_command(
        "/usr/bin/python3 -c " + shlex.quote(code),
        worktree=tmp_path,
        timeout_s=30,
        authority=acceptance.AcceptanceAuthority(primary=primary, test_database_url=None),
        source_write_files=("target.py",),
    )
    exit_code, stderr = result["exit_code"], str(result)
    if exit_code == 1 and "No permissions to create a new namespace" in stderr:
        pytest.skip("nested acceptance sandbox cannot create a PID namespace")
    assert exit_code == 0, stderr
    assert (tmp_path / "target.py").read_text() == "allowed"
    assert (tmp_path / "sibling.py").read_text() == "untouched"
    assert not (tmp_path / "new-sibling.py").exists()


@pytest.mark.parametrize(
    "repair,exit_code,stderr,expected",
    [
        (
            True,
            1,
            "bwrap: No permissions to create a new namespace",
            "acceptance-environment",
        ),
        (True, 1, "AssertionError: incorrect repair", None),
        (False, 1, "bwrap: No permissions to create a new namespace", None),
        (True, 0, "bwrap: No permissions to create a new namespace", None),
    ],
)
def test_exact_repair_namespace_failure_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repair: bool,
    exit_code: int,
    stderr: str,
    expected: str | None,
) -> None:
    acceptance = module

    (tmp_path / "fastapi_backend/.venv").mkdir(parents=True)
    (tmp_path / "target.py").write_text("original")
    _track_repair_fixture(tmp_path)
    monkeypatch.setattr(acceptance, "current_boundary_reusable", lambda **kwargs: False)
    monkeypatch.setattr(
        acceptance, "sandbox_command", lambda argv, **kwargs: ["bwrap", "--", *argv]
    )
    monkeypatch.setattr(
        acceptance,
        "subprocess",
        SimpleNamespace(
            run=lambda argv, **kwargs: (
                subprocess.CompletedProcess(argv, exit_code, "", stderr)
                if argv[0] == "bwrap"
                else subprocess.run(argv, **kwargs)
            ),
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    result = acceptance._run_one_acceptance_command(
        "true",
        worktree=tmp_path,
        timeout_s=20,
        authority=acceptance.AcceptanceAuthority(
            primary=tmp_path, test_database_url=None
        ),
        source_write_files=("target.py",) if repair else None,
    )
    assert result["exit_code"] == exit_code
    assert result.get("failure_class") == expected
