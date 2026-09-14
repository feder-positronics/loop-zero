"""Evidence-contract tests using committed fixtures, never imported candidate code."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import git

from loopzero.cli import main
from loopzero.code_health import analyzers
from loopzero.code_health.collector import collect
from loopzero.code_health.source import physical_lines


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-b", "main")
    return tmp_path


def commit(root, files):
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    git(root, "add", ".")
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD").strip()


def policy(*signals):
    return {
        "scope": ["."],
        "test_root": ["tests"],
        "exclude": ["generated/*"],
        "signals": ["size", *signals],
        "min_lines": 5,
        "min_tokens": 25,
        "clone_mode": "weak",
    }


def test_immutable_deterministic_and_separate_corpora(repository):
    sha = commit(
        repository,
        {
            "app.py": "async def run():\n    def nested():\n        return 1\n    return nested()",
            "tests/test_app.py": "assert True\n",
            "generated/sdk.py": "def sdk(): pass\n",
            "notes.md": "hello",
        },
    )
    (repository / "app.py").write_text("invalid dirty worktree!!!")
    first = collect(repository, sha, policy())
    assert first == collect(repository, sha, policy())
    assert first["valid"]
    head = first["head"]
    assert head["totals"]["production"] == {"files": 1, "lines": 4}
    assert head["totals"]["tests"]["lines"] == 1
    assert [(f["name"], f["span"]) for f in head["functions"]] == [
        ("run", 4),
        ("run.nested", 2),
    ]
    assert len(head["excluded"]) == 2


def test_comparison_growth_rename_and_ambiguous(repository):
    base = commit(
        repository,
        {"app.py": "def run():\n    return 1\ndef rename_me():\n    return 2\n"},
    )
    head = commit(
        repository,
        {
            "app.py": "def run():\n    x = 1\n    return x\ndef renamed():\n    return 2\ndef same(): pass\ndef same(): pass\n"
        },
    )
    result = collect(repository, head, policy(), base)["comparison"]
    assert len(result["changes"]) == 1
    assert result["changes"][0]["delta"] == 1
    assert len(result["unmatched_head_functions"]) == 3
    assert len(result["unmatched_base_functions"]) == 1


def test_partial_parse_and_tool_failure_preserve_inventory(repository, monkeypatch):
    head = commit(repository, {"good.py": "def f(): pass\n", "bad.py": "def ???"})

    def fail(*args):
        raise subprocess.TimeoutExpired("ruff", 180)

    monkeypatch.setattr(analyzers, "complexity", fail)
    result = collect(repository, head, policy("complexity"))
    assert not result["valid"]
    assert len(result["head"]["errors"]) == 2
    assert "size" not in result["head"]["completed"]
    assert result["head"]["functions"][0]["complexity"] is None


def test_missing_ref_writes_invalid_artifact(repository, tmp_path):
    output = tmp_path / "result.json"
    assert (
        main(
            [
                "--root",
                str(repository),
                "code-health",
                "snapshot",
                "--head",
                "missing",
                "--scope",
                ".",
                "--signals",
                "size",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert json.loads(output.read_text())["valid"] is False


def test_invalid_clone_ranges_retained_and_not_ranked(tmp_path):
    files = {"a.py": {"text": "x=1\nx=2\n", "lines": 2}}
    good = {"name": "source/a.py", "start": 1, "end": 2}
    for bad in (
        {**good, "end": 0},
        {**good, "end": 3},
        {**good, "name": "../a.py"},
        {**good, "start": True},
    ):
        pairs, errors = analyzers.validate_clones(
            {"duplicates": [{"firstFile": good, "secondFile": bad}]},
            files,
            tmp_path / "source",
        )
        assert not pairs
        assert errors[0]["raw"]["secondFile"] == bad


def test_reported_exact_does_not_assert_equal_lines(tmp_path):
    files = {
        "a.py": {"text": "a = 1", "lines": 1},
        "b.py": {"text": "b = 1", "lines": 1},
    }
    pairs, errors = analyzers.validate_clones(
        {
            "duplicates": [
                {
                    "kind": "exact",
                    "firstFile": {"name": "a.py", "start": 1, "end": 1},
                    "secondFile": {"name": "b.py", "start": 1, "end": 1},
                }
            ]
        },
        files,
        tmp_path,
    )
    assert not errors
    assert not pairs[0]["whole_lines_equal"]


def test_missing_optional_tool_is_invalid_not_clean(repository, monkeypatch):
    sha = commit(repository, {"app.py": "def f(): return 1\n"})

    def missing(*args, **kwargs):
        raise ValueError("ruff unavailable")

    monkeypatch.setattr(analyzers, "tool", missing)
    report = collect(repository, sha, policy("complexity"))
    assert not report["valid"]
    assert report["head"]["completed"] == ["size"]
    assert report["head"]["functions"][0]["complexity"] is None


def test_classification_preserves_policy_and_noise():
    assert (
        analyzers.classify("FIELDS = {'one': 'value'}", "a.py")
        == "policy-data-candidate"
    )
    assert analyzers.classify("import os\nimport sys", "a.py") == "imports"
    assert analyzers.classify('"docstring"', "a.py") == "documentation"
    assert analyzers.classify("result = execute()", "a.py") != "policy-data-candidate"
    assert (
        analyzers.classify("    FIELDS = {'one': 'value'}", "a.py")
        == "policy-data-candidate"
    )


def test_physical_lines_do_not_split_form_feeds():
    assert physical_lines("a\f b\nlast") == ["a\f b", "last"]
    assert physical_lines("a\r\nb\r") == ["a", "b"]
    assert physical_lines("") == []


def test_real_ruff_retains_valid_files_and_overrides_excludes(repository):
    require_tool("ruff")
    sha = commit(
        repository, {"dist/good.py": "def f():\n\f    return 1\n", "bad.py": "def ???"}
    )
    report = collect(repository, sha, policy("complexity"))
    assert not report["valid"]
    assert report["head"]["functions"][0]["complexity"] == 1
    assert report["head"]["files"]["dist/good.py"]["lines"] == 2
    assert "complexity" in report["head"]["completed"]


@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_real_ruff_modern_python_and_bom(repository, bom):
    require_tool("ruff")
    sha = commit(
        repository,
        {
            "modern.py": bom
            + "# ruff: noqa\ndef f(value):\n    match value:\n        case 1:\n            return True\n        case _:\n            return False\n"
        },
    )
    report = collect(repository, sha, policy("complexity"))
    assert report["valid"], report["head"]["errors"]
    assert report["head"]["functions"][0]["complexity"] is not None


def test_ruff_parse_gap_preserves_other_files(tmp_path, monkeypatch):
    from loopzero.code_health.python_metrics import functions

    source = tmp_path / "source"
    source.mkdir()
    inventory = []
    for name in ("good.py", "unsupported.py"):
        (source / name).write_text("def f(): return 1\n")
        inventory.extend(functions(name, (source / name).read_text()))
    diagnostics = [
        {
            "filename": str(tmp_path / "python/good.py"),
            "location": {"row": 1},
            "code": "C901",
            "message": "`f` is too complex (1 > 0)",
        },
        {
            "filename": str(tmp_path / "python/unsupported.py"),
            "location": {"row": 1},
            "code": None,
            "message": "unsupported syntax",
        },
    ]
    monkeypatch.setattr(
        analyzers, "tool", lambda *args, **kwargs: json.dumps(diagnostics)
    )
    errors = analyzers.complexity(tmp_path, inventory)
    assert inventory[0]["complexity"] == 1
    assert inventory[1]["complexity"] is None
    assert any("unsupported.py" in error for error in errors)


def require_tool(name):
    if not (Path(__import__("sys").executable).parent / name).exists():
        pytest.skip("optional code-health extra is not installed")


def test_real_ruff_all_functions_ignores_candidate_config(repository):
    require_tool("ruff")
    sha = commit(
        repository,
        {
            "app.py": "def f(x):\n    def nested():\n        return 1\n    if x:\n        return nested()\n    return 0\n",
            "ruff.toml": 'exclude = ["*"]\n',
        },
    )
    report = collect(repository, sha, policy("complexity"))
    assert report["valid"], report["head"]["errors"]
    assert [f["complexity"] for f in report["head"]["functions"]] == [3, 1]


def test_real_clone_new_copy_of_unchanged_source(repository):
    require_tool("jscpd")
    source = "def resolve(values):\n    result = []\n    for value in values:\n        if value is not None:\n            result.append(str(value).strip())\n    return sorted(set(result))\n"
    base = commit(repository, {"a.py": source})
    head = commit(
        repository,
        {"b.py": source, "generated/sdk.py": source, "tests/test_copy.py": source},
    )
    report = collect(repository, head, policy("duplication"), base)
    assert report["valid"], report["head"]["errors"]
    leads = report["comparison"]["clone_candidates"]
    assert leads
    assert {x["path"] for x in leads[0]["locations"]} == {"a.py", "b.py"}
    assert {x["path"]: x["fragment_seen_in_base"] for x in leads[0]["locations"]} == {
        "a.py": True,
        "b.py": False,
    }
    same = collect(repository, head, policy("duplication"), head)
    assert not same["comparison"]["highlights"]
    assert same["base"] == same["head"]


def test_overlapping_pairs_form_one_family():
    def loc(path, start, end):
        return {"path": path, "line": start, "end": end, "fingerprint": path}

    pairs = [
        {"locations": [loc("a.py", 1, 10), loc("b.py", 1, 10)]},
        {"locations": [loc("b.py", 8, 15), loc("c.py", 1, 8)]},
    ]
    assert len(analyzers.families(pairs)) == 1


def test_real_typescript_clones(repository):
    require_tool("jscpd")
    source = "export function resolve(values: string[]) {\n  const result: string[] = [];\n  for (const value of values) {\n    if (value.length > 0) {\n      result.push(value.trim());\n    }\n  }\n  return result.sort();\n}\n"
    sha = commit(repository, {"a.ts": source, "b.ts": source})
    report = collect(repository, sha, policy("duplication"))
    assert report["valid"]
    assert report["head"]["duplication"]["production"]["families"]
    assert report["head"]["coverage"]["complexity_unsupported"] == ["a.ts", "b.ts"]
