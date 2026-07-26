import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def load_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[4]
    module_path = repo_root / "scripts" / "hooks" / "size_lint.py"
    spec = importlib.util.spec_from_file_location("size_lint", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = load_module()


def test_count_lines_matches_wc_l_semantics(tmp_path: Path) -> None:
    no_trailing = tmp_path / "a.py"
    no_trailing.write_bytes(b"a\nb\nc")
    assert module.count_lines(str(no_trailing)) == 2

    with_trailing = tmp_path / "b.py"
    with_trailing.write_bytes(b"a\nb\nc\n")
    assert module.count_lines(str(with_trailing)) == 3


def test_warn_threshold_classifies_scope() -> None:
    assert module.warn_threshold("fastapi_backend/app/services/x.py") == 800
    assert module.warn_threshold("nextjs-frontend/components/X.tsx") == 400
    assert module.warn_threshold("nextjs-frontend/lib/x.ts") == 400
    # Out of scope -> None
    assert module.warn_threshold("fastapi_backend/tests/unit/x.py") is None
    assert module.warn_threshold("scripts/util/x.py") is None
    assert module.warn_threshold("docs/x.md") is None
    assert module.warn_threshold("nextjs-frontend/x.css") is None


def test_is_exempt() -> None:
    assert module.is_exempt("fastapi_backend/app/parsers/jats.py") is True
    assert module.is_exempt("fastapi_backend/app/foo/__tests__/x.py") is True
    assert module.is_exempt("nextjs-frontend/app/x.test.tsx") is True
    assert module.is_exempt("nextjs-frontend/app/openapi-client/sdk.gen.ts") is True
    assert (
        module.is_exempt(
            "fastapi_backend/app/scripts/run_structured_pdf_engine_benchmark.py"
        )
        is False
    )
    assert module.is_exempt("fastapi_backend/app/services/x.py") is False


def test_evaluate_warn_and_fail_tiers() -> None:
    findings = module.evaluate(
        {
            "fastapi_backend/app/services/at_limit.py": 800,  # boundary: OK
            "fastapi_backend/app/services/warn.py": 801,  # warn
            "fastapi_backend/app/services/fail.py": 1601,  # fail
            "nextjs-frontend/components/Warn.tsx": 401,  # warn
            "nextjs-frontend/components/Fail.tsx": 801,  # fail
            "fastapi_backend/app/parsers/big.py": 5000,  # exempt
            "docs/notes.md": 9999,  # out of scope
        }
    )
    by_path = {f.path: f for f in findings}
    assert set(by_path) == {
        "fastapi_backend/app/services/warn.py",
        "fastapi_backend/app/services/fail.py",
        "nextjs-frontend/components/Warn.tsx",
        "nextjs-frontend/components/Fail.tsx",
    }
    assert by_path["fastapi_backend/app/services/warn.py"].level == "warn"
    assert by_path["fastapi_backend/app/services/warn.py"].limit == 800
    assert by_path["fastapi_backend/app/services/fail.py"].level == "fail"
    assert by_path["fastapi_backend/app/services/fail.py"].limit == 1600
    assert by_path["nextjs-frontend/components/Warn.tsx"].limit == 400
    assert by_path["nextjs-frontend/components/Fail.tsx"].level == "fail"
    assert by_path["nextjs-frontend/components/Fail.tsx"].limit == 800


def test_main_fails_on_hard_limit(tmp_path: Path, capsys) -> None:
    big = tmp_path / "fastapi_backend" / "app" / "services" / "huge.py"
    big.parent.mkdir(parents=True)
    big.write_bytes(b"x\n" * 1700)
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
    f.write_bytes(b"x\n" * 900)
    rel = "fastapi_backend/app/services/warn.py"
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert module.main([rel]) == 0
    finally:
        os.chdir(cwd)
    err = capsys.readouterr().err
    assert "⚠" in err and "✗" not in err


def test_allowlist_entries_exist() -> None:
    """Stale-allowlist guard: every exempt path must still exist."""
    repo_root = Path(__file__).resolve().parents[4]
    missing = [p for p in module.ALLOWLIST_FILES if not (repo_root / p).exists()]
    assert missing == [], f"ALLOWLIST_FILES entries no longer exist: {missing}"
