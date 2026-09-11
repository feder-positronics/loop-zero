import pytest
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def load_module() -> ModuleType:
    return importlib.import_module('loopzero.hooks.size_lint')


module = load_module()

@pytest.fixture(autouse=True)
def legacy_size_policy(monkeypatch):
    from . import legacy_size_policy as policy
    values = dict(REPO_ROOT=Path.cwd(), BACKEND_ROOT="fastapi_backend/",
                  BACKEND_APP_ROOT="fastapi_backend/app/", SCRIPTS_ROOT="scripts/",
                  TEST_ROOT="fastapi_backend/tests/", FRONTEND_ROOT="nextjs-frontend/",
                  ALLOWLIST_PREFIXES=policy.ALLOWLIST_PREFIXES,
                  COHESIVE_FILES=policy.COHESIVE_FILES, BASELINE_FILES=policy.BASELINE_FILES,
                  ALLOWLIST_FILES=policy.ALLOWLIST_FILES)
    for name, value in values.items():
        monkeypatch.setattr(module, name, value)



def test_count_lines_matches_wc_l_semantics(tmp_path: Path) -> None:
    no_trailing = tmp_path / "a.py"
    no_trailing.write_bytes(b"a\nb\nc")
    assert module.count_lines(str(no_trailing)) == 2

    with_trailing = tmp_path / "b.py"
    with_trailing.write_bytes(b"a\nb\nc\n")
    assert module.count_lines(str(with_trailing)) == 3


def test_warn_threshold_classifies_scope() -> None:
    assert module.warn_threshold("fastapi_backend/app/services/x.py") == 800
    assert module.warn_threshold("scripts/util/x.py") == 800
    assert module.warn_threshold("scripts/hooks/size_lint.py") == 800
    assert module.warn_threshold("nextjs-frontend/components/X.tsx") == 400
    assert module.warn_threshold("nextjs-frontend/lib/x.ts") == 400
    # Python test files get their own (higher) threshold
    assert module.warn_threshold("fastapi_backend/tests/unit/x.py") == 2000
    assert module.warn_threshold("fastapi_backend/tests/conftest.py") == 2000
    assert module.warn_threshold("scripts/util/test_skill_convergence.py") == 2000
    # Stray test files under app/ are still in scope, at the test threshold
    assert module.warn_threshold("fastapi_backend/app/scripts/test_manual.py") == 2000
    # Out of scope -> None
    assert module.warn_threshold("docs/x.md") is None
    assert module.warn_threshold("nextjs-frontend/x.css") is None
    assert module.warn_threshold("fastapi_backend/conftest.py") is None


def test_is_python_test() -> None:
    assert module.is_python_test("fastapi_backend/tests/unit/services/x.py") is True
    assert module.is_python_test("scripts/util/test_skill_convergence.py") is True
    assert module.is_python_test("fastapi_backend/app/services/x.py") is False
    assert module.is_python_test("scripts/util/agent_dispatch.py") is False
    assert module.is_python_test("nextjs-frontend/lib/x.test.ts") is False


def test_is_exempt() -> None:
    assert module.is_exempt("fastapi_backend/app/parsers/jats.py") is True
    assert module.is_exempt("fastapi_backend/app/foo/__tests__/x.py") is True
    assert module.is_exempt("nextjs-frontend/app/x.test.tsx") is True
    assert module.is_exempt("nextjs-frontend/app/openapi-client/sdk.gen.ts") is True
    # Baseline ratchet entries
    assert module.is_exempt("scripts/util/agent_dispatch.py") is True
    assert (
        module.is_exempt("fastapi_backend/tests/unit/scripts/test_agent_dispatch.py")
        is True
    )
    # In scope, not exempt
    assert (
        module.is_exempt(
            "fastapi_backend/app/scripts/run_structured_pdf_engine_benchmark.py"
        )
        is False
    )
    assert module.is_exempt("fastapi_backend/app/services/x.py") is False
    assert module.is_exempt("scripts/util/job_helper.py") is False
    assert module.is_exempt("fastapi_backend/tests/unit/services/test_x.py") is False


def test_evaluate_warn_and_fail_tiers() -> None:
    findings = module.evaluate(
        {
            "fastapi_backend/app/services/at_limit.py": 800,  # boundary: OK
            "fastapi_backend/app/services/warn.py": 801,  # warn
            "fastapi_backend/app/services/fail.py": 1601,  # fail
            "scripts/util/warn_tool.py": 801,  # warn
            "scripts/util/fail_tool.py": 1601,  # fail
            "fastapi_backend/tests/unit/test_at_limit.py": 2000,  # boundary: OK
            "fastapi_backend/tests/unit/test_warn.py": 2001,  # warn
            "fastapi_backend/tests/unit/test_fail.py": 4001,  # fail
            "nextjs-frontend/components/Warn.tsx": 401,  # warn
            "nextjs-frontend/components/Fail.tsx": 801,  # fail
            "fastapi_backend/app/parsers/big.py": 5000,  # exempt
            "scripts/util/agent_dispatch.py": 22000,  # baseline exempt
            "docs/notes.md": 9999,  # out of scope
        }
    )
    by_path = {f.path: f for f in findings}
    assert set(by_path) == {
        "fastapi_backend/app/services/warn.py",
        "fastapi_backend/app/services/fail.py",
        "scripts/util/warn_tool.py",
        "scripts/util/fail_tool.py",
        "fastapi_backend/tests/unit/test_warn.py",
        "fastapi_backend/tests/unit/test_fail.py",
        "nextjs-frontend/components/Warn.tsx",
        "nextjs-frontend/components/Fail.tsx",
    }
    assert by_path["fastapi_backend/app/services/warn.py"].level == "warn"
    assert by_path["fastapi_backend/app/services/warn.py"].limit == 800
    assert by_path["fastapi_backend/app/services/fail.py"].level == "fail"
    assert by_path["fastapi_backend/app/services/fail.py"].limit == 1600
    assert by_path["scripts/util/fail_tool.py"].level == "fail"
    assert by_path["scripts/util/fail_tool.py"].limit == 1600
    assert by_path["fastapi_backend/tests/unit/test_warn.py"].limit == 2000
    assert by_path["fastapi_backend/tests/unit/test_fail.py"].level == "fail"
    assert by_path["fastapi_backend/tests/unit/test_fail.py"].limit == 4000
    assert by_path["nextjs-frontend/components/Warn.tsx"].limit == 400
    assert by_path["nextjs-frontend/components/Fail.tsx"].level == "fail"
    assert by_path["nextjs-frontend/components/Fail.tsx"].limit == 800


def test_evaluate_near_hard_boundary_is_distinct_and_non_blocking() -> None:
    findings = module.evaluate(
        {
            "fastapi_backend/app/services/near.py": 1440,
            "scripts/util/near.py": 1440,
            "fastapi_backend/tests/unit/test_near.py": 3600,
            "nextjs-frontend/components/Near.tsx": 720,
            "scripts/util/at_hard.py": 1600,
            "fastapi_backend/tests/unit/test_at_hard.py": 4000,
            "nextjs-frontend/components/AtHard.tsx": 800,
            "fastapi_backend/app/services/below.py": 1439,
        }
    )

    by_path = {finding.path: finding for finding in findings}
    assert set(by_path) == {
        "fastapi_backend/app/services/near.py",
        "scripts/util/near.py",
        "fastapi_backend/tests/unit/test_near.py",
        "nextjs-frontend/components/Near.tsx",
        "scripts/util/at_hard.py",
        "fastapi_backend/tests/unit/test_at_hard.py",
        "nextjs-frontend/components/AtHard.tsx",
        "fastapi_backend/app/services/below.py",
    }
    assert {path: finding.level for path, finding in by_path.items()} == {
        "fastapi_backend/app/services/near.py": "near-hard-limit",
        "scripts/util/near.py": "near-hard-limit",
        "fastapi_backend/tests/unit/test_near.py": "near-hard-limit",
        "nextjs-frontend/components/Near.tsx": "near-hard-limit",
        "scripts/util/at_hard.py": "near-hard-limit",
        "fastapi_backend/tests/unit/test_at_hard.py": "near-hard-limit",
        "nextjs-frontend/components/AtHard.tsx": "near-hard-limit",
        "fastapi_backend/app/services/below.py": "warn",
    }
    assert {path: finding.limit for path, finding in by_path.items()} == {
        "fastapi_backend/app/services/near.py": 1440,
        "scripts/util/near.py": 1440,
        "fastapi_backend/tests/unit/test_near.py": 3600,
        "nextjs-frontend/components/Near.tsx": 720,
        "scripts/util/at_hard.py": 1440,
        "fastapi_backend/tests/unit/test_at_hard.py": 3600,
        "nextjs-frontend/components/AtHard.tsx": 720,
        "fastapi_backend/app/services/below.py": 800,
    }


def test_health_inventory_lists_every_guarded_file_at_boundary(
    monkeypatch, capsys
) -> None:
    counts = {
        "scripts/util/near.py": 1440,
        "fastapi_backend/tests/unit/test_over.py": 4001,
        "nextjs-frontend/components/Below.tsx": 719,
        "scripts/util/agent_dispatch.py": 30_000,
        "docs/large.md": 50_000,
    }
    monkeypatch.setattr(module, "tracked_paths", lambda: list(counts))
    counted: list[str] = []

    def count_lines(path: Path) -> int:
        relative_path = str(path.relative_to(module.REPO_ROOT))
        counted.append(relative_path)
        return counts[relative_path]

    monkeypatch.setattr(module, "count_lines", count_lines)

    assert module.health_inventory() == 0

    output = capsys.readouterr().out
    assert "near-hard-limit\tscripts/util/near.py\t1440\t1440\t1600" in output
    assert "fail\tfastapi_backend/tests/unit/test_over.py\t4001\t3600\t4000" in output
    assert "Below.tsx" not in output
    assert "agent_dispatch.py" not in output
    assert counted == [
        "scripts/util/near.py",
        "fastapi_backend/tests/unit/test_over.py",
        "nextjs-frontend/components/Below.tsx",
    ]


def test_health_inventory_fails_when_a_guarded_file_cannot_be_read(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(module, "tracked_paths", lambda: ["scripts/util/near.py"])

    def unreadable(_path: Path) -> int:
        raise OSError("permission denied")

    monkeypatch.setattr(module, "count_lines", unreadable)

    assert module.health_inventory() == 1
    assert "cannot read guarded file scripts/util/near.py" in capsys.readouterr().err


def test_health_inventory_skips_a_tracked_file_missing_from_the_worktree(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(module, "tracked_paths", lambda: ["scripts/util/missing.py"])

    def missing(_path: Path) -> int:
        raise FileNotFoundError

    monkeypatch.setattr(module, "count_lines", missing)

    assert module.health_inventory() == 0
    captured = capsys.readouterr()
    assert "missing.py" not in captured.out
    assert captured.err == ""


def test_function_findings_flags_long_functions() -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(200))
    source = f"def long_fn():\n{body}\n\n\ndef short_fn():\n    return 1\n"
    findings = module.function_findings("scripts/util/tool.py", source)
    assert len(findings) == 1
    assert "def long_fn" in findings[0].path
    assert findings[0].lines == 201
    assert findings[0].limit == module.FUNCTION_WARN
    assert findings[0].level == "warn"


def test_function_findings_counts_methods_and_survives_syntax_error() -> None:
    body = "\n".join(f"        y{i} = {i}" for i in range(200))
    source = f"class C:\n    def long_method(self):\n{body}\n"
    findings = module.function_findings("scripts/util/tool.py", source)
    assert len(findings) == 1
    assert "def long_method" in findings[0].path
    assert module.function_findings("scripts/util/broken.py", "def (:\n") == []
    assert module.function_findings("scripts/util/nul.py", "x = 1\x00\n") == []


def test_evaluate_functions_skips_tests_and_exempt(tmp_path: Path) -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(200))
    source = f"def long_fn():\n{body}\n"
    for rel in (
        "scripts/util/tool.py",
        "scripts/util/test_tool.py",  # python test: skipped
        "scripts/util/agent_dispatch.py",  # allowlisted: skipped
        "docs/tool.py",  # out of scope: skipped
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        findings = module.evaluate_functions(
            [
                "scripts/util/tool.py",
                "scripts/util/test_tool.py",
                "scripts/util/agent_dispatch.py",
                "docs/tool.py",
            ]
        )
    finally:
        os.chdir(cwd)
    assert len(findings) == 1
    assert findings[0].path.startswith("scripts/util/tool.py:")


def test_main_fails_on_hard_limit(tmp_path: Path, capsys) -> None:
    big = tmp_path / "fastapi_backend" / "app" / "services" / "huge.py"
    big.parent.mkdir(parents=True)
    big.write_bytes(b"x = 1\n" * 1700)
    # Pass a repo-relative path the classifier recognizes by running from tmp_path.
    rel = "fastapi_backend/app/services/huge.py"
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert module.main([rel]) == 1
    finally:
        os.chdir(cwd)
    assert "✗" in capsys.readouterr().err


def test_main_warns_but_passes(tmp_path: Path, capsys) -> None:
    f = tmp_path / "fastapi_backend" / "app" / "services" / "warn.py"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x = 1\n" * 900)
    rel = "fastapi_backend/app/services/warn.py"
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert module.main([rel]) == 0
    finally:
        os.chdir(cwd)
    err = capsys.readouterr().err
    assert "⚠" in err and "✗" not in err


def test_main_function_warning_is_non_blocking(tmp_path: Path, capsys) -> None:
    body = "\n".join(f"    x{i} = {i}" for i in range(200))
    f = tmp_path / "scripts" / "util" / "tool.py"
    f.parent.mkdir(parents=True)
    f.write_text(f"def long_fn():\n{body}\n")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert module.main(["scripts/util/tool.py"]) == 0
    finally:
        os.chdir(cwd)
    err = capsys.readouterr().err
    assert "function soft limit" in err and "✗" not in err


@pytest.mark.skip(reason="Consumer exemption paths stay in intelflo; injected size-policy behavior is tested separately")
def test_allowlist_entries_exist() -> None:
    """Stale-allowlist guard: every exempt path must still exist."""
    repo_root = Path(__file__).resolve().parents[3]
    missing = [p for p in module.ALLOWLIST_FILES if not (repo_root / p).exists()]
    assert missing == [], f"ALLOWLIST_FILES entries no longer exist: {missing}"


def test_baseline_entries_are_still_over_their_hard_limit() -> None:
    """Ratchet guard: a decomposed baseline file must lose its exemption."""
    repo_root = Path(__file__).resolve().parents[3]
    stale = []
    for path in module.BASELINE_FILES:
        warn = module.warn_threshold(path)
        assert warn is not None, f"baseline entry out of scope: {path}"
        if not (repo_root / path).exists():
            continue  # test_allowlist_entries_exist owns the missing-file signal
        lines = module.count_lines(str(repo_root / path))
        if lines <= warn * module.HARD_MULTIPLIER:
            stale.append(f"{path} ({lines} lines)")
    assert stale == [], (
        "Baseline files now under their hard limit — remove their "
        f"BASELINE_FILES entries to lock in the win: {stale}"
    )


def _precommit_hook_block(hook_id: str) -> str:
    """Extract one hook's config block from .pre-commit-config.yaml."""
    repo_root = Path(__file__).resolve().parents[3]
    config = (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    # Anchor on the full "- id: <hook>" line so one hook id being a prefix of
    # another cannot select the wrong block.
    anchor = f"- id: {hook_id}\n"
    assert anchor in config, f"hook {hook_id} missing from pre-commit config"
    return config.split(anchor, 1)[1].split("- id:", 1)[0]


def _precommit_size_lint_scope() -> tuple[str, str]:
    """Extract the size-lint hook's files/exclude regexes from pre-commit config."""
    hook_block = _precommit_hook_block("size-lint")
    files_re = exclude_re = ""
    for line in hook_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("files:"):
            files_re = stripped.removeprefix("files:").strip().strip("\"'")
        elif stripped.startswith("exclude:"):
            exclude_re = stripped.removeprefix("exclude:").strip().strip("\"'")
    assert files_re and exclude_re
    return files_re, exclude_re


@pytest.mark.skip(reason='Consumer pre-commit wiring belongs to A4/init integration')
def test_warn_capable_precommit_hooks_are_verbose() -> None:
    """Hooks that print non-blocking notices and exit 0 need verbose: true,
    or pre-commit suppresses their output in passing runs."""
    import re

    for hook_id in (
        "size-lint",
        "staging-base-check",
        "docs-link-check",
        "commit-author-check",
    ):
        # Match a real config line, not a comment mentioning the attribute
        # (the extracted block absorbs the next hook's leading comments).
        assert re.search(
            r"^\s*verbose: true\s*$",
            _precommit_hook_block(hook_id),
            re.MULTILINE,
        ), (
            f"{hook_id} must set verbose: true or its exit-0 notices are "
            "invisible in passing runs"
        )


@pytest.mark.skip(reason='Consumer pre-commit wiring belongs to A4/init integration')
def test_precommit_filter_agrees_with_hook_classifier() -> None:
    """Every path the pre-commit filter admits must be classified by the hook.

    One-directional by design: the hook may classify more than the filter
    delivers (direct invocations pass arbitrary paths), but never less.
    """
    import re

    files_re, exclude_re = _precommit_size_lint_scope()
    representative = [
        "fastapi_backend/app/services/x.py",
        "fastapi_backend/app/scripts/test_manual.py",
        "fastapi_backend/tests/unit/services/test_x.py",
        "fastapi_backend/tests/conftest.py",
        "fastapi_backend/alembic_migrations/versions/abc123_x.py",
        "fastapi_backend/conftest.py",
        "scripts/util/agent_dispatch.py",
        "scripts/util/test_skill_convergence.py",
        "scripts/hooks/size_lint.py",
        "nextjs-frontend/components/X.tsx",
        "nextjs-frontend/lib/x.test.ts",
        "nextjs-frontend/app/openapi-client/sdk.gen.ts",
        "docs/notes.md",
    ]
    admitted_paths = [
        path
        for path in representative
        if re.search(files_re, path) and not re.search(exclude_re, path)
    ]
    # Positive control: if extraction or the regex breaks, fail loudly instead
    # of silently checking nothing.
    assert "fastapi_backend/app/services/x.py" in admitted_paths
    assert "scripts/util/agent_dispatch.py" in admitted_paths
    for path in admitted_paths:
        classified = module.warn_threshold(path) is not None
        skipped = any(tok in path for tok in module.SKIP_SUBSTRINGS)
        assert (
            classified or skipped
        ), f"pre-commit admits {path} but the hook never checks it"
