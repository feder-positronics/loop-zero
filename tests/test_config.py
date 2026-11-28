from __future__ import annotations

import json

import pytest

from loopzero import _proc, config
from loopzero.types import Config, ResourceLimits, ReviewConfig
from .conftest import commit_file, git

MINIMAL = '[repo]\nname = "o/n"\n'
COMMANDS = ("uv run pytest -q", "uv run ruff check .")
CHECK_INPUTS = {COMMANDS[0]: ("src/**",), "*": ("lock",)}


def write(tmp_path, text):
    path = tmp_path / "workflow.toml"
    path.write_text(text)
    return path


def table(section, **settings):
    return f"[{section}]\n" + "".join(f"{json.dumps(key)} = {json.dumps(value)}\n" for key, value in settings.items())


@pytest.mark.parametrize("strategy,queue", [("rebase", None), ("mergify", "main")])
def test_load_full_config(tmp_path, strategy, queue):
    review = ReviewConfig("gpt-5.6-luna", "medium", ("low", "medium", "high"))
    env = {"UV_CACHE_DIR": "/home/me/.cache/uv", "UV_PYTHON_DOWNLOADS": "never"}
    execution = dict(required_ci=("checks",), network=True, env_allowlist=("PATH", "HOME"),
                     writable=("/home/me/.cache/uv",), scratch=(".venv",))
    delivery = dict(reviewers=("codex",), reviewer_ro_paths=("/opt/claude", "/opt/codex"))
    limits = dict(memory_mb=1024, file_mb=512)
    workflow = "".join((
        table("repo", name="owner/name", base="develop"), table("checks", commands=COMMANDS,
              ro_paths=["/nonexistent/cache", "/tmp/whatever/../cache"], **execution),
        table("checks.env", **env), table("checks.limits", **limits), table("checks.inputs", **CHECK_INPUTS),
        table("delivery", merge=strategy, **({"mergify_queue": queue} if queue else {}), **delivery),
        table("delivery.review.codex", model=review.model, effort=review.effort, allowed_efforts=review.allowed_efforts),
    ))
    assert config.load(write(tmp_path, workflow)) == Config(
        repo="owner/name", base_branch="develop", checks=COMMANDS, merge_strategy=strategy, mergify_queue=queue,
        review={"codex": review}, sandbox_ro=("/nonexistent/cache", "/tmp/whatever/../cache"), env=tuple(env.items()),
        limits=ResourceLimits(**limits), inputs=CHECK_INPUTS, **execution, **delivery,
    )
    padded = workflow.replace('"uv run pytest -q"', '" uv run pytest -q "')
    assert config.load(write(tmp_path, padded)).checks[0] == " uv run pytest -q "


def test_trusted_load_applies_defaults_with_object_replacement_disabled(git_repo, monkeypatch):
    commit_file(git_repo, "workflow.toml", MINIMAL, "base workflow")
    git(git_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(git_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    run = _proc.run

    def check_object_access(argv, **kwargs):
        if argv[1] in {"rev-parse", "show"}:
            assert (kwargs.get("extra_env") or {}).get("GIT_NO_REPLACE_OBJECTS") == "1"
        return run(argv, **kwargs)

    monkeypatch.setattr(_proc, "run", check_object_access)
    trusted = config.load_trusted(git_repo)
    assert trusted.trusted_base_revision == git(git_repo, "rev-parse", "HEAD").strip()
    assert trusted == config.load(git_repo / "workflow.toml") == Config(
        repo="o/n", base_branch="main", checks=(), required_ci=(), merge_strategy="squash",
        reviewers=("claude", "codex"), network=False, env_allowlist=("PATH", "HOME", "LANG", "LC_ALL", "TERM"),
        sandbox_ro=(), writable=(), reviewer_ro_paths=(),
        scratch=(".venv", ".ruff_cache", ".pytest_cache", "node_modules/.cache"),
        limits=ResourceLimits(memory_mb=4096, file_mb=2048),
    )


@pytest.mark.parametrize("text,message", [
    (None, "config file not found"),
    ("[repo\n", "invalid TOML"),
    ("[checks]\ncommands = []\n", "missing required key repo.name"),
    ('repo = "o/n"\n', r"\[repo\] must be a table"),
    ('[repo]\nname = 3\n', "repo.name must be a string"),
    *((table("repo", name=name), 'must look like "owner/name"')
      for name in ("noslash", "a/b/c", "a/", "a/b c", "a/b@c", "-a/@b", "a/b?x=1", "acme/..", "./widgets", "../w")),
    (MINIMAL + '[repo2]\nx = 1\n', r"unknown key\(s\) in top level: repo2"),
    (MINIMAL + 'nmae = "x"\n', r"unknown key\(s\) in \[repo\]: nmae"),
    (MINIMAL + 'base = ""\n', "repo.base must not be empty"),
    (MINIMAL + table("checks", commands=["echo ok"]) + table("checks.inputs", **{"echo okay": ["."]}),
     r'checks\.inputs\."echo okay".*exact'),
    (MINIMAL + table("checks", commands=COMMANDS) + table("checks.inputs", **{COMMANDS[0]: "src"}), r"checks\.inputs"),
    (MINIMAL + '[checks]\nenv = ["X=1"]\n', "checks.env must be a table"),
    (MINIMAL + '[checks]\nlimits = 1\n', "checks.limits must be a table"),
    *((MINIMAL + table(section, **options), message) for section, options, message in [
        ("checks", {"commands": "pytest"}, "checks.commands must be an array"),
        ("checks", {"commands": ["ok", 1]}, "non-empty strings"),
        ("checks", {"commands": ["true", "   "]}, r"workflow\.toml: checks\.commands.*nonblank command"),
        ("checks", {"network": "yes"}, "checks.network must be a boolean"),
        ("checks", {"ro_paths": ["/home/<you>/.cache"]}, "unreplaced example placeholder"),
        ("checks", {"writable": ["/run/x"]}, "writable must not expose"),
        ("checks", {"runner": ["runner"]}, r"unknown key\(s\) in \[checks\]: runner"),
        ("checks.env", {"lower": "x"}, "invalid variable name 'lower'"),
        ("checks.env", {"X": 1}, "checks.env.X must be a str"),
        ("checks.limits", {"memory_mb": 0}, "checks.limits.memory_mb must be a positive int"),
        ("checks.limits", {"cpu": 1}, r"unknown key\(s\) in checks.limits: cpu"),
        ("delivery", {"merge": "ff"}, "delivery.merge must be one of"),
        ("delivery", {"merge": "mergify"}, "mergify_queue is required"),
        ("delivery", {"reviewers": ["gpt"]}, "delivery.reviewers entries"),
        ("delivery", {"reviewers": []}, "at least one reviewer"),
        ("delivery", {"reviewer_ro_paths": ["relative"]}, "must be absolute"),
        ("delivery", {"review_chunk_bytes": 200000}, r"unknown key\(s\) in \[delivery\]: review_chunk_bytes"),
        ("delivery.review.codex", {"mode": "x"}, r"unknown key\(s\) in \[delivery.review.codex\]: mode"),
        ("delivery.review.codex", {"allowed_efforts": [1]}, "allowed_efforts must be a list of non-empty strings"),
        ("delivery.review.codex", {"allowed_efforts": "medium"}, "allowed_efforts must be an array"),
    ]),
    *((MINIMAL + table("delivery", mergify_queue="main", **options),
       rf"delivery\.mergify_queue.*delivery\.merge.*{selected}")
      for options, selected in [({}, "squash"), ({"merge": "squash"}, "squash"), ({"merge": "queue"}, "queue")]),
    *((MINIMAL + table("checks", ro_paths=[path]), "must not expose")
      for path in ("/", "/home", "/root", "/run", "/var/run", "/proc/1", "/dev", "/sys/fs", "/tmp/../run")),
    *((MINIMAL + table("checks", scratch=[path]), "scratch entries must be relative") for path in ("../x", "/abs")),
    *((MINIMAL + table("checks.env", **{key: "/x"}), f"must not override {key}") for key in ("PATH", "HOME")),
])
def test_invalid_content(tmp_path, text, message):
    path = tmp_path / "workflow.toml" if text is None else write(tmp_path, text)
    with pytest.raises(config.ConfigError, match=message) as info:
        config.load(path)
    assert str(info.value).startswith(str(path))


def test_ro_path_boundary(tmp_path):
    paths = ("/home/me/.local/bin", "/opt/tool", "/srv/cache<2026>/uv")
    assert config.load(write(tmp_path, MINIMAL + table("checks", ro_paths=paths))).sandbox_ro == paths


def test_base_fetch_failure_retains_redacted_git_diagnostic(tmp_path, monkeypatch):
    error = "fatal: Authentication failed for 'https://example-token@example.com/repo'\n"
    monkeypatch.setattr(config, "run_git", lambda *_args, **_kwargs: _proc.Completed(128, "", error, 0))
    with pytest.raises(config.ConfigError, match="could not refresh origin/main: fatal: Authentication failed") as exc:
        config._remote_workflow(tmp_path, "main", None, refresh=True, require_ci=False, read_timeout=lambda budget: budget)
    assert "[REDACTED]@example.com" in str(exc.value)
    assert "example-token" not in str(exc.value)
    assert "Verify network connectivity and access to origin/main, then rerun the command." in str(exc.value)
