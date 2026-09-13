from contextvars import Context
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

import loopzero.review as review
from loopzero.config import ConfigError
from loopzero.kernel import seams
from loopzero.review import (
    _acceptance_grammar,
    _ci_path_classifier,
    _security_scope,
    acceptance,
    authority,
    chain,
    evidence,
    findings,
    harness,
    risk,
    routing,
)
from loopzero.kernel import settings as kernel_settings


@pytest.fixture(autouse=True)
def isolated_process_configuration(monkeypatch):
    previous_kernel = kernel_settings.settings
    contexts = (
        (_acceptance_grammar._TOOLCHAIN, _acceptance_grammar._TOOLCHAIN.get()),
        (acceptance._PROFILE, acceptance._PROFILE.get()),
        (authority._ARCHIVE_VALIDATOR, authority._ARCHIVE_VALIDATOR.get()),
        (chain._PROFILE, chain._PROFILE.get()),
        (evidence._PROFILE, evidence._PROFILE.get()),
        (harness._AUTHORITY_APPEND, harness._AUTHORITY_APPEND.get()),
        (harness._PROFILE, harness._PROFILE.get()),
        (risk._SETTINGS, risk._SETTINGS.get()),
        (routing._SETTINGS, routing._SETTINGS.get()),
    )
    defaults = (
        (_acceptance_grammar, "_DEFAULT_TOOLCHAIN", _acceptance_grammar._DEFAULT_TOOLCHAIN),
        (acceptance, "_DEFAULT_PROFILE", acceptance._DEFAULT_PROFILE),
        (authority, "_DEFAULT_ARCHIVE_VALIDATOR", authority._DEFAULT_ARCHIVE_VALIDATOR),
        (chain, "_DEFAULT_PROFILE", chain._DEFAULT_PROFILE),
        (evidence, "_DEFAULT_PROFILE", evidence._DEFAULT_PROFILE),
        (harness, "_DEFAULT_AUTHORITY_APPEND", harness._DEFAULT_AUTHORITY_APPEND),
        (harness, "_DEFAULT_PROFILE", harness._DEFAULT_PROFILE),
        (risk, "_DEFAULT_SETTINGS", risk._DEFAULT_SETTINGS),
        (routing, "_DEFAULT_SETTINGS", routing._DEFAULT_SETTINGS),
    )
    old_findings = (
        findings.FINDINGS_DIR, findings.OPERATIONS_DIR, findings._SEVERITY_RANK,
        findings._SECURITY_PATH_PATTERNS, findings._CONFIGURED_ROOT,
        findings._REQUIRE_PR_SCOPE,
    )
    old_security_scope = (
        _security_scope._ALWAYS_SECURITY_REVIEW_PATTERNS,
        _security_scope._REQUIRED_SECTIONS,
    )
    old_path_classifier = (
        _ci_path_classifier.PATH_CLASSES,
        _ci_path_classifier.PARENT_CLASSES,
    )
    monkeypatch.setattr(review, "_DEFAULT_PROFILE", None)
    try:
        yield
    finally:
        kernel_settings.configure(previous_kernel)
        for context, value in contexts:
            context.set(value)
        for module, name, value in defaults:
            setattr(module, name, value)
        (
            findings.FINDINGS_DIR, findings.OPERATIONS_DIR, findings._SEVERITY_RANK,
            findings._SECURITY_PATH_PATTERNS, findings._CONFIGURED_ROOT,
            findings._REQUIRE_PR_SCOPE,
        ) = old_findings
        (
            _security_scope._ALWAYS_SECURITY_REVIEW_PATTERNS,
            _security_scope._REQUIRED_SECTIONS,
        ) = old_security_scope
        (
            _ci_path_classifier.PATH_CLASSES,
            _ci_path_classifier.PARENT_CLASSES,
        ) = old_path_classifier


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


def test_composition_root_configures_kernel_seams(tmp_path):
    profile = _profile(tmp_path)
    review.configure(profile)
    assert seams._latest_attempt_settlement_indices(
        [{"type": "inline", "task_id": "task"}]
    ) == frozenset({0})


def test_empty_context_worker_uses_process_review_configuration(tmp_path) -> None:
    profile = _profile(tmp_path)
    review.configure(profile)
    observed: dict[str, object] = {}
    failures: list[BaseException] = []

    def inspect_configuration() -> None:
        try:
            def inspect_empty_context() -> None:
                observed["routing"] = routing.settings().env_prefix
                observed["sections"] = chain._required_for_paths(())
                observed["security_sections"] = chain._required_for_paths(("auth.py",))
                observed["toolchain"] = acceptance._toolchain()
                observed["evidence"] = evidence._ref_namespaces()
                observed["harness"] = harness._configured_profile()

            Context().run(inspect_empty_context)
        except BaseException as exc:  # surfaced in the parent test thread
            failures.append(exc)

    worker = Thread(target=inspect_configuration)
    worker.start()
    worker.join()

    assert failures == []
    assert observed == {
        "routing": "CONSUMER",
        "sections": ("code",),
        "security_sections": ("code", "security"),
        "toolchain": profile.toolchain,
        "evidence": ("dispatch-snapshots", "finding-snapshots"),
        "harness": profile,
    }


def test_configuring_two_different_review_profiles_fails(tmp_path) -> None:
    profile = _profile(tmp_path)
    review.configure(profile)
    review.configure(profile)

    with pytest.raises(ConfigError, match="different Profile"):
        review.configure(_profile(tmp_path, env_prefix="OTHER"))


def test_review_configuration_preserves_resolved_kernel_interpreter(tmp_path) -> None:
    profile = _profile(tmp_path)
    resolved_interpreter = tmp_path / "uv" / "cpython-3.13.7" / "bin" / "python3.13"
    kernel_settings.configure(
        kernel_settings.KernelSettings(
            env_prefix=profile.env_prefix,
            toolchain={"interpreter": str(resolved_interpreter)},
        )
    )

    review.configure(profile)

    assert kernel_settings.settings.toolchain == {
        "interpreter": str(resolved_interpreter),
        "db_lock": "/tmp/db.lock",
    }
    assert kernel_settings.settings.audit_root == profile.audit_root
