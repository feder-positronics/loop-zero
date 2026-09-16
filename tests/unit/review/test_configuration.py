"""Process configuration is exercised before mechanism imports, as in consumers."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path


PROFILE_HELPER = """
def _profile(tmp_path: Path, *, env_prefix: str = "CONSUMER") -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path, env_prefix=env_prefix, audit_root=Path("private"),
        state_root=f"~/.local/state/{env_prefix.lower()}",
        toolchain={"interpreter": "tools/.venv/bin/python", "db_lock": "/tmp/db.lock"},
        aliases={}, tiers={}, routing_budgets={}, routing_policy_version="policy",
        telemetry_schema_version="telemetry", compatible_policy_versions=(),
        default_timeout_s=30, engine_cooldown_s=30,
        required_sections=("code", "security"),
        max_reviews_per_pr=1, max_delta_reviews=1,
        finding_severities=("critical", "important", "suggestion"),
        path_classes={}, path_class_parents={}, security_patterns=("**/auth/**",),
        github=SimpleNamespace(ref_namespace="refs/consumer"),
    )

"""


def probe(tmp_path, body):
    source = Path(__file__).resolve().parents[3] / "src"
    setup = (
        "import sys\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "import loopzero.review as review\n"
        "from loopzero.kernel import settings as kernel_settings\n"
        "from loopzero.config import ConfigError\n"
        + PROFILE_HELPER
        + "\n"
        + f"profile = _profile(Path({str(tmp_path)!r}))\n"
    )
    outcome = subprocess.run(
        [sys.executable, "-I", "-c", setup + textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("LOOPZERO_")
        },
    )
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr


def test_composition_root_configures_kernel_seams(tmp_path):
    probe(
        tmp_path,
        """
        review.configure(profile)
        from loopzero.kernel import seams
        assert seams._latest_attempt_settlement_indices(
            [{"type": "inline", "task_id": "task"}]
        ) == frozenset({0})
    """,
    )


def test_empty_context_worker_uses_process_review_configuration(tmp_path):
    probe(
        tmp_path,
        """
        from contextvars import Context
        from threading import Thread
        review.configure(profile)
        from loopzero.review import routing, chain, acceptance, evidence, harness
        from loopzero.kernel import sandbox, gitscope, policy, worktree_lease
        observed = []
        def inspect():
            observed.append((
                routing.settings().env_prefix,
                chain._required_for_paths(()),
                chain._required_for_paths(("auth.py",)),
                acceptance._toolchain(), evidence._ref_namespaces(),
                harness._configured_profile(),
                sandbox.settings.env_prefix, gitscope.DISPATCH_DIR,
                policy.AUTHORITY_LEDGER_DIRECTORY, worktree_lease.LEASE_FD_ENV,
            ))
        worker = Thread(target=lambda: Context().run(inspect))
        worker.start()
        worker.join()
        assert observed == [(
            "CONSUMER", ("code",), ("code", "security"), profile.toolchain,
            ("dispatch-snapshots", "finding-snapshots"), profile,
            "CONSUMER", Path("private/dispatch"), Path("private/dispatch/ledger"),
            "CONSUMER_WORKTREE_LEASE_FD",
        )], observed
    """,
    )


def test_configuring_two_different_review_profiles_fails(tmp_path):
    probe(
        tmp_path,
        """
        review.configure(profile)
        review.configure(profile)
        try:
            review.configure(_profile(profile.root, env_prefix="OTHER"))
        except ConfigError as exc:
            assert "different Profile" in str(exc)
        else:
            raise AssertionError("accepted another consumer")
    """,
    )


def test_review_configuration_preserves_resolved_kernel_interpreter(tmp_path):
    probe(
        tmp_path,
        """
        interpreter = str(profile.root / "approved/bin/python")
        kernel_settings.configure(kernel_settings.KernelSettings(
            env_prefix=profile.env_prefix,
            toolchain={"interpreter": interpreter},
        ))
        review.configure(profile)
        from loopzero.kernel import sandbox
        assert kernel_settings.settings.toolchain == {
            "interpreter": interpreter, "db_lock": "/tmp/db.lock",
        }
        assert sandbox.settings.toolchain == kernel_settings.settings.toolchain
        assert sandbox.settings.audit_root == profile.audit_root
    """,
    )


def test_late_configuration_is_rejected_before_mutation(tmp_path):
    probe(
        tmp_path,
        """
        from loopzero.kernel import sandbox
        before = kernel_settings.settings
        try:
            review.configure(profile)
        except ConfigError as exc:
            assert "before importing kernel mechanisms" in str(exc)
        else:
            raise AssertionError("accepted conflicting captured kernel settings")
        assert kernel_settings.settings is before
        assert review._DEFAULT_PROFILE is None
    """,
    )


def test_unconfigured_review_keeps_conservative_security_default(tmp_path):
    probe(
        tmp_path,
        """
        from loopzero.review import chain
        task = {"security_trigger_paths": [], "required_sections": ["code"]}
        try:
            chain.validate_review_chain_task(task)
        except ValueError:
            pass
        else:
            raise AssertionError("unconfigured review lost its security requirement")
        task["required_sections"].append("security")
        assert chain.validate_review_chain_task(task) == ("code", "security")
    """,
    )


def test_preconfigured_kernel_remains_supported(tmp_path):
    probe(
        tmp_path,
        """
        kernel_settings.configure(kernel_settings.KernelSettings.from_profile(profile))
        from loopzero.kernel import sandbox
        review.configure(profile)
        assert sandbox.settings == kernel_settings.settings
    """,
    )
