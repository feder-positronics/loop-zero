from __future__ import annotations

import pytest

from loopzero import config
from loopzero.types import Config, ResourceLimits, ReviewConfig

FULL = """
[repo]
name = "owner/name"
base = "develop"

[checks]
commands = ["uv run pytest -q", "uv run ruff check ."]
required_ci = ["checks"]
network = true
env_allowlist = ["PATH", "HOME"]
ro_paths = ["/nonexistent/cache", "/tmp/whatever/../cache"]
writable = ["/home/me/.cache/uv"]
scratch = [".venv"]
env = { UV_CACHE_DIR = "/home/me/.cache/uv", UV_PYTHON_DOWNLOADS = "never" }
limits = { memory_mb = 1024, file_mb = 512 }

[delivery]
merge = "rebase"
reviewers = ["codex"]
reviewer_ro_paths = ["/opt/claude", "/opt/codex"]
review_chunk_bytes = 12345

[delivery.review.codex]
model = "gpt-5.6-luna"
effort = "medium"
allowed_efforts = ["low", "medium", "high"]
"""


def write(tmp_path, text):
    path = tmp_path / "workflow.toml"
    path.write_text(text)
    return path


def test_load_full_config(tmp_path):
    assert config.load(write(tmp_path, FULL)) == Config(
        repo="owner/name",
        base_branch="develop",
        checks=("uv run pytest -q", "uv run ruff check ."),
        required_ci=("checks",),
        merge_strategy="rebase",
        reviewers=("codex",),
        review={"codex": ReviewConfig("gpt-5.6-luna", "medium", ("low", "medium", "high"))},
        reviewer_ro_paths=("/opt/claude", "/opt/codex"),
        review_chunk_bytes=12345,
        network=True,
        env_allowlist=("PATH", "HOME"),
        sandbox_ro=("/nonexistent/cache", "/tmp/whatever/../cache"),
        writable=("/home/me/.cache/uv",),
        scratch=(".venv",),
        env=(("UV_CACHE_DIR", "/home/me/.cache/uv"), ("UV_PYTHON_DOWNLOADS", "never")),
        limits=ResourceLimits(memory_mb=1024, file_mb=512),
    )

    padded = FULL.replace('"uv run pytest -q"', '" uv run pytest -q "')
    assert config.load(write(tmp_path, padded)).checks[0] == " uv run pytest -q "


def test_load_applies_defaults(tmp_path):
    loaded = config.load(write(tmp_path, '[repo]\nname = "o/n"\n'))
    assert loaded == Config(
        repo="o/n",
        base_branch="main",
        checks=(),
        required_ci=(),
        merge_strategy="squash",
        reviewers=("claude", "codex"),
    )
    assert loaded.network is False
    assert loaded.env_allowlist == ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
    assert loaded.sandbox_ro == () and loaded.writable == ()
    assert loaded.reviewer_ro_paths == ()
    assert loaded.scratch == (".venv", ".ruff_cache", ".pytest_cache", "node_modules/.cache")
    assert loaded.limits == ResourceLimits(memory_mb=4096, file_mb=2048)


def test_mergify_delivery_requires_and_loads_queue(tmp_path):
    loaded = config.load(write(tmp_path, """
[repo]
name = "o/n"
[delivery]
merge = "mergify"
mergify_queue = "main"
"""))
    assert loaded.merge_strategy == "mergify"
    assert loaded.mergify_queue == "main"

    with pytest.raises(config.ConfigError, match="mergify_queue is required"):
        config.load(write(tmp_path, '[repo]\nname = "o/n"\n[delivery]\nmerge = "mergify"\n'))


def test_missing_file(tmp_path):
    with pytest.raises(config.ConfigError, match="config file not found"):
        config.load(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[repo\n", "invalid TOML"),
        ("[checks]\ncommands = []\n", "missing required key repo.name"),
        ('[repo]\nname = "noslash"\n', 'must look like "owner/name"'),
        ('[repo]\nname = 3\n', "repo.name must be a str"),
        ('[repo]\nname = "o/n"\n[checks]\ncommands = "pytest"\n', "checks.commands must be a list"),
        ('[repo]\nname = "o/n"\n[checks]\ncommands = ["ok", 1]\n', "non-empty strings"),
        ('[repo]\nname = "o/n"\n[checks]\ncommands = ["true", "   "]\n',
         r"workflow\.toml: checks\.commands.*nonblank command"),
        ('[repo]\nname = "o/n"\n[checks]\nnetwork = "yes"\n', "checks.network must be a bool"),
        ('[repo]\nname = "o/n"\n[delivery]\nmerge = "ff"\n', "delivery.merge must be one of"),
        ('[repo]\nname = "o/n"\n[delivery]\nreviewers = ["gpt"]\n', "delivery.reviewers entries"),
        ('[repo]\nname = "o/n"\n[repo2]\nx = 1\n', "unknown key\\(s\\) in top level: repo2"),
        ('[repo]\nname = "o/n"\nnmae = "x"\n', "unknown key\\(s\\) in \\[repo\\]: nmae"),
        ('repo = "o/n"\n', "\\[repo\\] must be a table"),
        ('[repo]\nname = "o/n"\nbase = ""\n', "repo.base must not be empty"),
        ('[repo]\nname = "o/n"\n[delivery]\nreviewers = []\n', "at least one reviewer"),
        ('[repo]\nname = "o/n"\n[delivery]\nreviewer_ro_paths = ["relative"]\n', "must be absolute"),
        *[(f'[repo]\nname = "o/n"\n[delivery]\nreview_chunk_bytes = {value}\n', "int >= 4096")
          for value in (0, 1, 4095)],
        ('[repo]\nname = "o/n"\n[delivery.review.codex]\nmode = "x"\n',
         "unknown key\\(s\\) in \\[delivery.review.codex\\]: mode"),
        ('[repo]\nname = "o/n"\n[delivery.review.codex]\nallowed_efforts = [1]\n',
         "allowed_efforts must be a list of non-empty strings"),
        ('[repo]\nname = "o/n"\n[delivery.review.codex]\nallowed_efforts = "medium"\n',
         "allowed_efforts must be a list"),
        ('[repo]\nname = "o/n"\n[checks]\nro_paths = ["rel/path"]\n', "must be absolute"),
        ('[repo]\nname = "o/n"\n[checks]\nwritable = ["/run/x"]\n', "writable must not expose"),
        ('[repo]\nname = "o/n"\n[checks]\nscratch = ["../x"]\n', "scratch entries must be relative"),
        ('[repo]\nname = "o/n"\n[checks]\nscratch = ["/abs"]\n', "scratch entries must be relative"),
        ('[repo]\nname = "o/n"\n[checks.env]\nlower = "x"\n', "invalid variable name 'lower'"),
        ('[repo]\nname = "o/n"\n[checks.env]\nPATH = "/x"\n', "must not override PATH"),
        ('[repo]\nname = "o/n"\n[checks.env]\nHOME = "/x"\n', "must not override HOME"),
        ('[repo]\nname = "o/n"\n[checks.env]\nX = 1\n', "checks.env.X must be a str"),
        ('[repo]\nname = "o/n"\n[checks]\nenv = ["X=1"]\n', "checks.env must be a dict"),
        ('[repo]\nname = "o/n"\n[checks]\nlimits = 1\n', "checks.limits must be a dict"),
        ('[repo]\nname = "o/n"\n[checks]\nlimits = { memory_mb = 0 }\n',
         "checks.limits.memory_mb must be a positive int"),
        ('[repo]\nname = "o/n"\n[checks]\nlimits = { cpu = 1 }\n',
         "unknown key\\(s\\) in checks.limits: cpu"),
        ('[repo]\nname = "o/n"\n[checks]\ncommands = ["echo ok"]\n'
         '[checks.inputs]\n"echo okay" = ["."]\n', r'checks\.inputs\."echo okay".*exact'),
        *[
            ('[repo]\nname = "o/n"\n[checks]\nrunner = ["runner"]\ncommands = ["true"]\n'
             f"[checks.limits]\n{limit} = 1\n", rf"checks\.limits\.{limit}.*local")
            for limit in ("file_mb", "memory_mb")
        ],
    ],
)
def test_invalid_content(tmp_path, text, message):
    with pytest.raises(config.ConfigError, match=message) as info:
        config.load(write(tmp_path, text))
    assert str(info.value).startswith(str(tmp_path / "workflow.toml"))


FORBIDDEN = ["/", "/home", "/root", "/run", "/var/run", "/run/user/1000", "/proc/1", "/dev"]
FORBIDDEN += ["/var/run/docker.sock", "/sys/fs", "/tmp/../run"]


@pytest.mark.parametrize("path,allowed", [
    *((path, False) for path in FORBIDDEN),
    *((path, True) for path in ("/home/me/.local/bin", "/opt/tool", "/tmp/x")),
])
def test_ro_path_boundary(tmp_path, path, allowed):
    cfg = write(tmp_path, f'[repo]\nname = "o/n"\n[checks]\nro_paths = ["{path}"]\n')
    if allowed:
        assert config.load(cfg).sandbox_ro == (path,)
    else:
        with pytest.raises(config.ConfigError, match="must not expose"):
            config.load(cfg)


def test_check_inputs(tmp_path):
    cfg = config.load(write(tmp_path, FULL + '\n[checks.inputs]\n"uv run pytest -q" = ["src/**"]\n"*" = ["lock"]\n'))
    assert cfg.inputs == {"uv run pytest -q": ("src/**",), "*": ("lock",)}
    with pytest.raises(config.ConfigError):
        config.load(write(tmp_path, FULL + '\n[checks.inputs]\n"uv run pytest -q" = "src"\n'))


@pytest.mark.parametrize("strategy", ["", 'merge = "squash"\n', 'merge = "queue"\n'])
def test_mergify_queue_rejects_other_strategies(tmp_path, strategy):
    selected = "queue" if "queue" in strategy else "squash"
    text = '[repo]\nname = "o/n"\n[delivery]\n' + strategy + 'mergify_queue = "main"\n'
    with pytest.raises(config.ConfigError, match=rf"delivery\.mergify_queue.*delivery\.merge.*{selected}"):
        config.load(write(tmp_path, text))
