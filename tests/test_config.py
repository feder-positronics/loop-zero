from __future__ import annotations

import json

import pytest

from loopzero import _proc, config
from loopzero.types import Config, ResourceLimits, ReviewConfig

MINIMAL = '[repo]\nname = "o/n"\n'
COMMANDS = ("uv run pytest -q", "uv run ruff check .")
CHECK_INPUTS = {COMMANDS[0]: ("src/**",), "*": ("lock",)}


def write(tmp_path, text):
    path = tmp_path / "workflow.toml"
    path.write_text(text)
    return path


def table(section, **settings):
    # JSON strings, arrays, bools and integers are also valid TOML values.
    rows = [f"{json.dumps(key)} = {json.dumps(value)}" for key, value in settings.items()]
    return "\n".join([f"[{section}]", *rows, ""])


@pytest.mark.parametrize("strategy,queue", [("rebase", None), ("mergify", "main")])
def test_load_full_config(tmp_path, strategy, queue):
    review = ReviewConfig("gpt-5.6-luna", "medium", ("low", "medium", "high"))
    env = {"UV_CACHE_DIR": "/home/me/.cache/uv", "UV_PYTHON_DOWNLOADS": "never"}
    execution = dict(required_ci=("checks",), network=True, env_allowlist=("PATH", "HOME"),
                     writable=("/home/me/.cache/uv",), scratch=(".venv",))
    delivery = dict(reviewers=("codex",), reviewer_ro_paths=("/opt/claude", "/opt/codex"))
    limits = dict(memory_mb=1024, file_mb=512)
    workflow = "".join((
        table("repo", name="owner/name", base="develop"),
        table("checks", commands=COMMANDS, ro_paths=["/nonexistent/cache", "/tmp/whatever/../cache"], **execution),
        table("checks.env", **env), table("checks.limits", **limits),
        table("checks.inputs", **CHECK_INPUTS),
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


def test_load_applies_defaults(tmp_path):
    assert config.load(write(tmp_path, MINIMAL)) == Config(
        repo="o/n", base_branch="main", checks=(), required_ci=(), merge_strategy="squash",
        reviewers=("claude", "codex"), network=False, env_allowlist=("PATH", "HOME", "LANG", "LC_ALL", "TERM"),
        sandbox_ro=(), writable=(), reviewer_ro_paths=(),
        scratch=(".venv", ".ruff_cache", ".pytest_cache", "node_modules/.cache"),
        limits=ResourceLimits(memory_mb=4096, file_mb=2048),
    )


def test_missing_file(tmp_path):
    with pytest.raises(config.ConfigError, match="config file not found"):
        config.load(tmp_path / "nope.toml")


FORBIDDEN = ["/", "/home", "/root", "/run", "/var/run", "/run/user/1000", "/proc/1", "/dev",
             "/var/run/docker.sock", "/sys/fs", "/tmp/../run"]


@pytest.mark.parametrize("text,message", [
    ("[repo\n", "invalid TOML"),
    ("[checks]\ncommands = []\n", "missing required key repo.name"),
    ('[repo]\nname = "noslash"\n', 'must look like "owner/name"'),
    ('[repo]\nname = 3\n', "repo.name must be a string"),
    (MINIMAL + '[checks]\ncommands = "pytest"\n', "checks.commands must be an array"),
    (MINIMAL + '[checks]\ncommands = ["ok", 1]\n', "non-empty strings"),
    (MINIMAL + '[checks]\ncommands = ["true", "   "]\n',
     r"workflow\.toml: checks\.commands.*nonblank command"),
    (MINIMAL + '[checks]\nnetwork = "yes"\n', "checks.network must be a boolean"),
    (MINIMAL + '[delivery]\nmerge = "ff"\n', "delivery.merge must be one of"),
    (MINIMAL + '[delivery]\nmerge = "mergify"\n', "mergify_queue is required"),
    *((MINIMAL + f'[delivery]\n{strategy}mergify_queue = "main"\n',
       rf"delivery\.mergify_queue.*delivery\.merge.*{selected}")
      for strategy, selected in [("", "squash"), ('merge = "squash"\n', "squash"), ('merge = "queue"\n', "queue")]),
    (MINIMAL + '[checks]\ncommands = ["uv run pytest -q"]\n[checks.inputs]\n"uv run pytest -q" = "src"\n', r"checks\.inputs"),
    (MINIMAL + '[delivery]\nreviewers = ["gpt"]\n', "delivery.reviewers entries"),
    (MINIMAL + '[repo2]\nx = 1\n', "unknown key\\(s\\) in top level: repo2"),
    (MINIMAL + 'nmae = "x"\n', "unknown key\\(s\\) in \\[repo\\]: nmae"),
    ('repo = "o/n"\n', "\\[repo\\] must be a table"),
    (MINIMAL + 'base = ""\n', "repo.base must not be empty"),
    (MINIMAL + '[delivery]\nreviewers = []\n', "at least one reviewer"),
    (MINIMAL + '[delivery]\nreviewer_ro_paths = ["relative"]\n', "must be absolute"),
    (MINIMAL + '[delivery.review.codex]\nmode = "x"\n',
     "unknown key\\(s\\) in \\[delivery.review.codex\\]: mode"),
    (MINIMAL + '[delivery.review.codex]\nallowed_efforts = [1]\n',
     "allowed_efforts must be a list of non-empty strings"),
    (MINIMAL + '[delivery.review.codex]\nallowed_efforts = "medium"\n',
     "allowed_efforts must be an array"),
    (MINIMAL + '[checks]\nro_paths = ["rel/path"]\n', "must be absolute"),
    *((MINIMAL + table("checks", ro_paths=[path]), "must not expose") for path in FORBIDDEN),
    (MINIMAL + '[checks]\nwritable = ["/run/x"]\n', "writable must not expose"),
    *((MINIMAL + f'[checks]\nscratch = ["{path}"]\n', "scratch entries must be relative")
      for path in ("../x", "/abs")),
    (MINIMAL + '[checks.env]\nlower = "x"\n', "invalid variable name 'lower'"),
    *((MINIMAL + f'[checks.env]\n{key} = "/x"\n', f"must not override {key}")
      for key in ("PATH", "HOME")),
    (MINIMAL + '[checks.env]\nX = 1\n', "checks.env.X must be a str"),
    (MINIMAL + '[checks]\nenv = ["X=1"]\n', "checks.env must be a table"),
    (MINIMAL + '[checks]\nlimits = 1\n', "checks.limits must be a table"),
    (MINIMAL + '[checks]\nlimits = { memory_mb = 0 }\n',
     "checks.limits.memory_mb must be a positive int"),
    (MINIMAL + '[checks]\nlimits = { cpu = 1 }\n',
     "unknown key\\(s\\) in checks.limits: cpu"),
    (MINIMAL + '[checks]\ncommands = ["echo ok"]\n'
     '[checks.inputs]\n"echo okay" = ["."]\n', r'checks\.inputs\."echo okay".*exact'),
    (MINIMAL + '[checks]\nrunner = ["runner"]\n', "unknown key\\(s\\) in \\[checks\\]: runner"),
])
def test_invalid_content(tmp_path, text, message):
    with pytest.raises(config.ConfigError, match=message) as info:
        config.load(write(tmp_path, text))
    assert str(info.value).startswith(str(tmp_path / "workflow.toml"))


@pytest.mark.parametrize("path", ["/home/me/.local/bin", "/opt/tool", "/tmp/x"])
def test_ro_path_boundary(tmp_path, path):
    assert config.load(write(tmp_path, MINIMAL + table("checks", ro_paths=[path]))).sandbox_ro == (path,)


def test_base_fetch_failure_retains_redacted_git_diagnostic(tmp_path, monkeypatch):
    error = "fatal: Authentication failed for 'https://example-token@example.com/repo'\n"
    monkeypatch.setattr(config, "run_git", lambda *_args, **_kwargs: _proc.Completed(128, "", error, 0))
    with pytest.raises(config.ConfigError, match="could not refresh origin/main: fatal: Authentication failed") as exc:
        config._remote_workflow(tmp_path, "main", None, refresh=True, require_ci=False, read_timeout=lambda budget: budget)
    assert "[REDACTED]@example.com" in str(exc.value)
    assert "example-token" not in str(exc.value)
