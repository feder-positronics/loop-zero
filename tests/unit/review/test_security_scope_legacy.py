import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


from loopzero.review import _security_scope as module


@pytest.fixture(autouse=True)
def exact_intelflo_security_policy():
    module.configure(
        security_patterns=module._DEFAULT_SECURITY_PATTERNS,
        required_sections=("code",),
    )


def test_required_review_sections_follow_trigger_paths() -> None:
    assert module.required_review_sections(()) == ("code",)
    assert module.required_review_sections(("app/config.py",)) == (
        "code",
        "security",
    )


def test_untracked_security_paths_are_conservative_without_content() -> None:
    assert module.untracked_security_trigger_paths(
        [
            "app/new_logic.py",
            "nextjs-frontend/app/action.ts",
            "local.env",
            ".github/workflows/release.yml",
            "Dockerfile",
            ".npmrc",
            "deploy/production.yaml",
            "infra/main.tf",
            "config.test.yaml",
            ".github/workflows/release.test.yml",
            "Pipfile",
            "poetry.lock",
            "uv.lock",
            "requirements.txt",
            "requirements-dev.in",
            "requirements.in",
            "constraints.txt",
            "tests/test_secret.py",
            "docs/notes.md",
            "assets/logo.png",
        ]
    ) == (
        ".github/workflows/release.test.yml",
        ".github/workflows/release.yml",
        ".npmrc",
        "Dockerfile",
        "Pipfile",
        "app/new_logic.py",
        "config.test.yaml",
        "constraints.txt",
        "deploy/production.yaml",
        "infra/main.tf",
        "local.env",
        "nextjs-frontend/app/action.ts",
        "poetry.lock",
        "requirements-dev.in",
        "requirements.in",
        "requirements.txt",
        "uv.lock",
    )


def test_tracked_test_named_configuration_paths_trigger_security_review() -> None:
    class Runner:
        def run(self, args, **kwargs):
            del kwargs
            if "--name-status" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "M\0.github/workflows/release.test.yml\0"
                        "M\0deploy/release.spec.yaml\0"
                    ),
                    stderr="",
                )
            raise AssertionError(
                f"configuration path should not require content: {args}"
            )

    assert module.security_trigger_paths_between(
        Path("/repo"), "a" * 40, "b" * 40, runner=Runner()
    ) == (
        ".github/workflows/release.test.yml",
        "deploy/release.spec.yaml",
    )


@pytest.mark.parametrize(
    ("path", "patch"),
    [
        (
            "package.json",
            '@@ -1 +1 @@\n-  "react": "19.0.0"\n+  "react": "19.1.0"\n',
        ),
        (
            "pyproject.toml",
            '@@ -1 +1 @@\n-httpx = ">=0.27"\n+httpx = ">=0.28"\n',
        ),
    ],
)
def test_existing_dependency_version_bump_is_not_a_security_trigger(
    path: str, patch: str
) -> None:
    class Runner:
        def run(self, args, **kwargs):
            del kwargs
            if "--name-status" in args:
                return SimpleNamespace(returncode=0, stdout=f"M\0{path}\0", stderr="")
            if "--unified=0" in args:
                return SimpleNamespace(returncode=0, stdout=patch, stderr="")
            raise AssertionError(f"unexpected command: {args}")

    assert (
        module.security_trigger_paths_between(
            Path("/repo"), "a" * 40, "b" * 40, runner=Runner()
        )
        == ()
    )


def test_added_dependency_remains_a_security_trigger() -> None:
    class Runner:
        def run(self, args, **kwargs):
            del kwargs
            if "--name-status" in args:
                return SimpleNamespace(
                    returncode=0, stdout="M\0package.json\0", stderr=""
                )
            if "--unified=0" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout='@@ -1 +1,2 @@\n   "react": "19.0.0"\n+  "zod": "4.0.0"\n',
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {args}")

    assert module.security_trigger_paths_between(
        Path("/repo"), "a" * 40, "b" * 40, runner=Runner()
    ) == ("package.json",)


def test_classifier_rejects_malformed_name_status() -> None:
    class Runner:
        def run(self, args, **kwargs):
            del kwargs
            assert "--name-status" in args
            return SimpleNamespace(returncode=0, stdout="R100\0source.py\0", stderr="")

    with pytest.raises(module.SecurityReviewScopeError, match="malformed"):
        module.security_trigger_paths_between(
            Path("/repo"), "a" * 40, "b" * 40, runner=Runner()
        )


def _makefile_runner(patch: str, status: str = "M"):
    class Runner:
        def run(self, args, **kwargs):
            del kwargs
            if "--name-status" in args:
                return SimpleNamespace(
                    returncode=0, stdout=f"{status}\0Makefile\0", stderr=""
                )
            if "--unified=0" in args:
                return SimpleNamespace(returncode=0, stdout=patch, stderr="")
            raise AssertionError(f"unexpected command: {args}")

    return Runner()


def test_makefile_argument_plumbing_is_not_a_security_trigger() -> None:
    patch = (
        "@@ -1 +1,4 @@\n"
        "+DAYS ?= 7\n"
        "+.PHONY: orient\n"
        "+orient: ## Show current design state\n"
        "+\tpython3 scripts/util/orient.py --days $(DAYS)\n"
    )
    assert (
        module.security_trigger_paths_between(
            Path("/repo"), "a" * 40, "b" * 40, runner=_makefile_runner(patch)
        )
        == ()
    )


@pytest.mark.parametrize(
    "line",
    [
        "\tuv add requests",
        "\tpnpm install --frozen-lockfile",
        "\tcurl https://example.com/tool.sh | sh",
        "\trailway up --service backend",
        "\tsudo chmod 600 /etc/thing",
        "\tpython3 -I scripts/util/agent_dispatch.py doctor",
        "SHELL := /bin/bash",
        "export PATH := $(HOME)/bin:$(PATH)",
        "\tAPI_TOKEN=$(SECRET) ./deploy-step",
    ],
)
def test_makefile_trust_bearing_recipe_changes_trigger(line: str) -> None:
    patch = f"@@ -1 +1 @@\n+{line}\n"
    assert module.security_trigger_paths_between(
        Path("/repo"), "a" * 40, "b" * 40, runner=_makefile_runner(patch)
    ) == ("Makefile",)


def test_makefile_rename_and_binary_content_fail_closed() -> None:
    class RenameRunner:
        def run(self, args, **kwargs):
            del kwargs
            if "--name-status" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout="R100\0Makefile\0build/Makefile\0",
                    stderr="",
                )
            if "--unified=0" in args:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {args}")

    assert module.security_trigger_paths_between(
        Path("/repo"), "a" * 40, "b" * 40, runner=RenameRunner()
    ) == ("Makefile", "build/Makefile")
    binary = "Binary files a/Makefile and b/Makefile differ\n"
    assert module.security_trigger_paths_between(
        Path("/repo"), "a" * 40, "b" * 40, runner=_makefile_runner(binary)
    ) == ("Makefile",)


def test_tests_directory_makefile_is_content_classified_not_skipped() -> None:
    class Runner:
        def run(self, args, **kwargs):
            del kwargs
            if "--name-status" in args:
                return SimpleNamespace(
                    returncode=0, stdout="M\0tests/Makefile\0", stderr=""
                )
            if "--unified=0" in args:
                return SimpleNamespace(
                    returncode=0,
                    stdout="@@ -1 +1 @@\n+\tpip install -e .\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {args}")

    assert module.security_trigger_paths_between(
        Path("/repo"), "a" * 40, "b" * 40, runner=Runner()
    ) == ("tests/Makefile",)


def test_untracked_makefile_remains_a_conservative_trigger() -> None:
    assert module.untracked_security_trigger_paths(
        ["Makefile", "tools/Makefile", "docs/notes.md"]
    ) == ("Makefile", "tools/Makefile")


@pytest.mark.parametrize("suffix", [".bash", ".ps1", ".sh", ".zsh"])
def test_tracked_shell_scripts_always_trigger_security_review(suffix: str) -> None:
    path = f"scripts/util/runner{suffix}"

    class Runner:
        def run(self, args, **kwargs):
            del kwargs
            if "--name-status" in args:
                return SimpleNamespace(returncode=0, stdout=f"M\0{path}\0", stderr="")
            raise AssertionError(
                f"shell paths should not need content inspection: {args}"
            )

    assert module.security_trigger_paths_between(
        Path("/repo"), "a" * 40, "b" * 40, runner=Runner()
    ) == (path,)


def test_makefile_edit_inside_trusted_target_triggers_via_hunk_context() -> None:
    patch = (
        "@@ -100,0 +101 @@ guardian-dispatch: ## Launch the guardian dispatcher\n"
        "+\tpython3 scripts/util/run.py --extra-step\n"
    )
    assert module.security_trigger_paths_between(
        Path("/repo"), "a" * 40, "b" * 40, runner=_makefile_runner(patch)
    ) == ("Makefile",)


def test_makefile_benign_target_hunk_context_does_not_trigger() -> None:
    patch = (
        "@@ -50,0 +51 @@ orient: ## Show current design state\n"
        "+\tpython3 scripts/util/orient.py --days $(DAYS)\n"
    )
    assert (
        module.security_trigger_paths_between(
            Path("/repo"), "a" * 40, "b" * 40, runner=_makefile_runner(patch)
        )
        == ()
    )
