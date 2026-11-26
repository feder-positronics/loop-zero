from __future__ import annotations

import errno
import itertools
import json
import os
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import tracemalloc
import traceback
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from select import select

import pytest

from loopzero import cli, sandbox
from loopzero.cli import _current_check_report, _save_report
from loopzero.types import Config, ResourceLimits

from .conftest import git

FAKE_BWRAP = '''#!/usr/bin/python3
import os
import sys

args = sys.argv[1:]
with open("{log}", "w", encoding="utf-8") as stream:
    stream.writelines(f"{{arg}}\\n" for arg in args)
separator = args.index("--")
options, command = args[:separator], args[separator + 1:]
env = {{}} if "--clearenv" in options else dict(os.environ)
home_source = None
i = 0
while i < len(options):
    if options[i] == "--setenv":
        env[options[i + 1]] = options[i + 2]
        i += 3
    elif options[i] == "--bind" and options[i + 2] == "/tmp/home":
        home_source = options[i + 1]
        i += 3
    else:
        i += 1
if home_source:
    command = [
        home_source + arg.removeprefix("/tmp/home") if arg.startswith("/tmp/home/") else arg
        for arg in command
    ]
    if env.get("CODEX_HOME") == "/tmp/home":
        env["CODEX_HOME"] = home_source
    if env.get("HOME") == "/tmp/home":
        env["HOME"] = home_source
os.chdir(options[options.index("--chdir") + 1])
os.execvpe(command[0], command, env)
'''


def make_config(**overrides) -> Config:
    values = {"repo": "o/n", "base_branch": "main", "checks": ("echo hello",), "required_ci": ()}
    values |= {"merge_strategy": "squash", "reviewers": ("claude",)}
    return Config(**{**values, "env_allowlist": ("PATH", "HOME", "LZ_KEEP"), **overrides})


@pytest.fixture
def bwrap_log(fake_tool, tmp_path) -> Path:
    log = tmp_path / "bwrap.argv"
    fake_tool("bwrap", FAKE_BWRAP.format(log=log))
    return log


def argv_of(log: Path) -> list[str]:
    """argv of the last bwrap invocation (the probe runs first, checks overwrite it)."""
    return log.read_text().splitlines()


def test_report_head_dirty_and_results(git_repo, bwrap_log):
    report = sandbox.run_checks(
        make_config(checks=("echo one", "echo two; exit 4", "echo never")), git_repo
    )
    assert report.head == git(git_repo, "rev-parse", "HEAD").strip()
    assert report.dirty is False
    (git_repo / "README.md").write_text("changed\n")
    assert sandbox.run_checks(make_config(), git_repo).dirty is True
    assert [(r.command, r.exit_code, r.tail) for r in report.results] == [
        ("echo one", 0, "one"),
        ("echo two; exit 4", 4, "two"),
    ]
    assert report.ok is False


def test_report_fails_when_head_changes_during_run(git_repo, bwrap_log):
    before = git(git_repo, "rev-parse", "HEAD").strip()
    command = "git -c user.name=test -c user.email=test@example.com commit --allow-empty -m drift"

    report = sandbox.run_checks(make_config(checks=(command,)), git_repo)

    after = git(git_repo, "rev-parse", "HEAD").strip()
    assert after != before
    assert report.head == before and report.dirty is True and report.ok is False
    drift = report.results[-1]
    assert (drift.command, drift.exit_code) == ("<worktree changed during run>", 1)
    assert before in drift.tail and after in drift.tail


@pytest.mark.parametrize("command,exit_code,line_count,start,end", [
    ("for i in $(seq 1 50); do echo $i; done; echo err >&2", 0, 40, "12", "err"),
    ("""python3 -c 'print("x" * 20000 + " Ω end")'""", 0, 1, "x", " Ω end"),
    ("""python3 -c 'print("terminal line" + "\\n" * 20_000)'""", 0, 1, "terminal line", "terminal line"),
    ("./" * 8_200 + "absent/file", 1, 1, "", "otherwise restore the required file."),
], ids=["lines", "oversized-line", "trailing-newlines", "missing-file"])
def test_tail_merges_streams_and_truncates(git_repo, bwrap_log, command, exit_code, line_count, start, end):
    (result,) = sandbox.run_checks(make_config(checks=(command,)), git_repo).results
    lines = result.tail.splitlines()
    assert (result.exit_code, len(lines)) == (exit_code, line_count)
    assert lines[0].startswith(start) and lines[-1].endswith(end)
    assert len(result.tail.encode("utf-8")) <= 16 * 1024


def test_verbose_stage_bounds_parent_memory(git_repo):
    command = shlex.join([sys.executable, "-c", """
import os
import sys

block = b"x" * (1024 * 1024)
for _ in range(50):
    sys.stdout.buffer.write(block)
sys.stdout.flush()
os.write(1, b"\\r\\nlast stdout\\r\\n")
os.write(2, "final Ω diagnostic\\r\\n".encode())
sys.exit(7)
"""])
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        baseline, _ = tracemalloc.get_traced_memory()
        result = sandbox._run_one(make_config(), git_repo, [], command, 30)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.exit_code == 7
    assert result.tail.endswith("\nlast stdout\nfinal Ω diagnostic")
    assert len(result.tail.encode()) == 16 * 1024
    assert peak - baseline < 4 * 1024 * 1024


@pytest.mark.parametrize(
    ("command", "stage_exit"),
    [
        ("/bin/sh -c 'exit 7' | /bin/cat", 7),
        ("/bin/sh -c 'kill -TERM $$' | /bin/cat", 143),
        ("echo ok | /bin/cat", 0),
        ("echo ok", 0),
    ],
)
def test_check_pipeline_verdict(git_repo, bwrap_log, monkeypatch, capsys, command, stage_exit):
    config = make_config(checks=(command,))
    monkeypatch.setattr(cli, "_context", lambda *args, **kwargs: (git_repo, config))
    monkeypatch.setattr(cli, "_refresh_pr_checks", lambda *args: None)

    assert cli.main(["check"]) == (1 if stage_exit else 0)
    receipt = json.loads((git_repo / ".loopzero/checks.json").read_text())
    assert [stage["exit_code"] for stage in receipt["results"]] == [stage_exit]
    assert capsys.readouterr().out.splitlines()[-1] == ("FAIL" if stage_exit else "PASS")


def test_bwrap_flags(git_repo, bwrap_log, monkeypatch):
    monkeypatch.setenv("LZ_KEEP", "kept")
    monkeypatch.setenv("LZ_DROP", "dropped")
    sandbox.run_checks(make_config(), git_repo)
    argv = argv_of(bwrap_log)
    head, tail = argv[: argv.index("--")], argv[argv.index("--") + 1 :]
    for flag in ("--die-with-parent", "--new-session", "--unshare-all", "--clearenv"):
        assert flag in head
    assert "--share-net" not in head
    assert head[head.index("--cap-drop") + 1] == "ALL"
    system = [p for p in sandbox.SYSTEM_RO if Path(p).exists()]
    assert _pairs(head, "--ro-bind") == [(p, p) for p in system] + [(str(git_repo),) * 2]
    assert "/" not in system and "/home" not in system
    assert _pairs(head, "--ro-bind-try") == []
    windows = list(itertools.pairwise(head))
    assert all(("--tmpfs", p) in windows for p in ("/tmp", "/run", "/var/run"))
    assert ("--dev", "/dev") in windows and ("--proc", "/proc") in windows
    assert head[head.index("--chdir") + 1] == str(git_repo)
    setenv = dict(_pairs(head, "--setenv"))
    assert setenv["PATH"] == os.environ["PATH"] and setenv["LZ_KEEP"] == "kept"
    assert "LZ_DROP" not in setenv
    assert setenv["HOME"] == sandbox.SANDBOX_HOME
    assert setenv["UV_CACHE_DIR"] == "/tmp/uv-cache" and setenv["PYTHONDONTWRITEBYTECODE"] == "1"
    binds = _pairs(head, "--bind")
    home_src, home_dst = binds[0]
    assert home_dst == sandbox.SANDBOX_HOME and Path(home_src).name.startswith("loopzero-home-")
    assert Path(home_src) != Path(os.environ.get("HOME", "/"))
    # Scratch dirs use a separate, unexposed host root.
    scratch = {dst: src for src, dst in binds[1:]}
    assert list(scratch) == [str(git_repo / e) for e in make_config().scratch]
    assert all("loopzero-scratch-" in src for src in scratch.values())
    assert all(not src.startswith(home_src + "/") for src in scratch.values())
    assert head.index(str(git_repo)) < head.index(str(git_repo / ".venv"))
    assert (git_repo / "node_modules/.cache").is_dir()
    assert sandbox.run_checks(make_config(), git_repo).dirty is False
    # /tmp tmpfs comes before the worktree bind so a worktree under /tmp stays visible.
    assert head.index("--tmpfs") < head.index(str(git_repo))
    assert tail == ["prlimit", f"--as={sandbox._cap_limit(4096 << 20, resource.RLIMIT_AS)}",
                    f"--fsize={sandbox._cap_limit(2048 << 20, resource.RLIMIT_FSIZE)}",
                    "--", "/bin/bash", "--posix", "-o", "pipefail", "-c", "echo hello"]


def test_probe_uses_real_flags(git_repo, bwrap_log):
    sandbox.run_checks(make_config(checks=(), network=True), git_repo)
    argv = argv_of(bwrap_log)  # only the probe ran
    assert argv[argv.index("--") + 1 :] == ["true"]
    assert argv.index("--unshare-all") < argv.index("--share-net") < argv.index("--clearenv")


def test_network_binds_resolv_conf_after_run_tmpfs(git_repo, bwrap_log):
    sandbox.run_checks(make_config(network=True), git_repo)
    argv = argv_of(bwrap_log)
    resolv_conf = Path("/etc/resolv.conf")
    resolved = resolv_conf.resolve()
    if resolv_conf.is_symlink() and any(
        resolved.is_relative_to(root) for root in (Path("/tmp"), Path("/run"))
    ):
        source = destination = str(resolved.parent)
    else:
        source, destination = str(resolved), str(resolv_conf)
    bind_at = next(
        i
        for i in range(len(argv) - 2)
        if argv[i : i + 3] == ["--ro-bind", source, destination]
    )
    run_tmpfs_at = next(
        i for i in range(len(argv) - 1) if argv[i : i + 2] == ["--tmpfs", "/run"]
    )
    assert bind_at > run_tmpfs_at


def test_extra_paths_and_env_override(git_repo, bwrap_log, tmp_path, monkeypatch):
    extra, cache = tmp_path / "ro", tmp_path / "rw"
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.setenv("RUFF_CACHE_DIR", "host-ruff")
    cfg = make_config(sandbox_ro=(str(extra),), writable=(str(cache),), scratch=())
    cfg = Config(**{**cfg.__dict__, "env_allowlist": ("PATH", "UV_CACHE_DIR", "RUFF_CACHE_DIR")})
    sandbox.run_checks(Config(**{**cfg.__dict__, "env": (("RUFF_CACHE_DIR", "cfg"),)}), git_repo)
    argv = argv_of(bwrap_log)
    assert _pairs(argv, "--ro-bind-try") == [(str(extra),) * 2]
    assert _pairs(argv, "--bind-try") == [(str(cache),) * 2]
    assert len(_pairs(argv, "--bind")) == 1  # only HOME; no scratch
    setenv = dict(_pairs(argv, "--setenv"))  # defaults < host allowlist < config.env
    assert (setenv["UV_CACHE_DIR"], setenv["RUFF_CACHE_DIR"]) == (str(cache), "cfg")


def test_temp_parent_cannot_be_exposed(git_repo, bwrap_log):
    with pytest.raises(sandbox.SandboxUnavailable, match="private check directories"):
        sandbox.run_checks(make_config(writable=(tempfile.gettempdir(),)), git_repo)


def inject_check_timeout(monkeypatch, output, before_timeout=None):
    def read_output_tail(process, limit, timeout):
        if before_timeout is not None:
            before_timeout()
        raise subprocess.TimeoutExpired(process.args, timeout, output=output[-limit:])

    monkeypatch.setattr(sandbox._proc, "_read_output_tail", read_output_tail)


def test_local_stage_timeout_retains_drained_output(git_repo):
    command = shlex.join([sys.executable, "-c", """
import os
import signal

os.write(1, b"x" * 20_000)
os.write(2, b"\\nlast available diagnostic\\n")
signal.pause()
"""])
    result = sandbox._run_one(make_config(), git_repo, [], command, 1)
    assert result.exit_code == sandbox.TIMEOUT_EXIT
    assert result.tail.endswith("\nlast available diagnostic\n\n[loopzero] timed out after 1s")
    assert len(result.tail.encode()) == 16 * 1024


def test_timeout_is_recorded_not_raised(git_repo, bwrap_log, monkeypatch):
    inject_check_timeout(monkeypatch, b"x" * 20_000 + b"partial")
    report = sandbox.run_checks(
        make_config(checks=("echo partial", "echo after")), git_repo, timeout=0.3
    )
    (first,) = report.results
    assert (first.exit_code, len(first.tail.encode("utf-8"))) == (sandbox.TIMEOUT_EXIT, 16 * 1024)
    assert "partial\n[loopzero] timed out" in first.tail and not report.ok


def test_configured_resource_limits_are_converted_to_bytes(git_repo, bwrap_log, monkeypatch):
    limits = ResourceLimits(memory_mb=64, file_mb=3)
    monkeypatch.setattr(resource, "getrlimit", lambda _: (0, 32 * 1024 * 1024))
    sandbox.run_checks(make_config(limits=limits), git_repo)
    argv = argv_of(bwrap_log)
    assert argv[argv.index("--") + 1:] == [
        "prlimit", "--as=33554432", "--fsize=3145728",
        "--", "/bin/bash", "--posix", "-o", "pipefail", "-c", "echo hello",
    ]


def test_linked_worktree_binds_common_git_dir(git_repo, bwrap_log, tmp_path):
    linked = tmp_path / "linked"
    git(git_repo, "worktree", "add", "-q", str(linked), "-b", "feature")
    sandbox.run_checks(make_config(), linked)
    binds = _pairs(argv_of(bwrap_log), "--ro-bind")
    common = (git_repo / ".git").resolve()
    assert binds[-2:] == [(str(linked.resolve()),) * 2, (str(common),) * 2]


def test_scratch_rejects_symlink_parent_before_mkdir(git_repo, bwrap_log, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (git_repo / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sandbox.SandboxUnavailable, match="symlink component"):
        sandbox.run_checks(make_config(scratch=("linked/cache",)), git_repo)
    assert not (outside / "cache").exists()


@pytest.mark.parametrize("entry", [".", ".git", ".git/cache"])
def test_scratch_rejects_worktree_and_git_metadata(git_repo, bwrap_log, entry):
    with pytest.raises(sandbox.SandboxUnavailable, match=r"unsafe|Git metadata"):
        sandbox.run_checks(make_config(scratch=(entry,)), git_repo)


def test_bwrap_missing_or_probe_failure(git_repo, fake_bin, fake_tool, monkeypatch):
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(fake_bin))
    with pytest.raises(sandbox.SandboxUnavailable) as missing:
        sandbox.run_checks(make_config(), git_repo)
    assert "bwrap binary missing" in str(missing.value)
    assert "sudo apt install bubblewrap" in str(missing.value)
    monkeypatch.setenv("PATH", path)
    fake_tool("bwrap", "echo 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    with pytest.raises(sandbox.SandboxUnavailable) as denied:
        sandbox.run_checks(make_config(), git_repo)
    assert "namespace creation failed" in str(denied.value)
    assert "exit 1" in str(denied.value)
    assert "No permissions to create new namespace" not in str(denied.value)
    assert (
        "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" in str(denied.value)
    )


def test_probe_timeout_reports_only_the_timeout(git_repo, monkeypatch):
    def timed_out(*args, **kwargs):
        raise sandbox._proc.ProcTimeout("private probe diagnostic")

    monkeypatch.setattr(sandbox._proc, "run", timed_out)
    with pytest.raises(sandbox.SandboxUnavailable) as info:
        sandbox.probe(make_config(), git_repo, ["bwrap", "--"])

    assert str(info.value) == f"bwrap namespace creation timed out after {sandbox.GIT_TIMEOUT:g}s"
    assert info.value.__suppress_context__


def test_alternate_probe_timeout_traceback_omits_process_diagnostics(git_repo, monkeypatch):
    def timed_out(*args, **kwargs):
        raise sandbox._proc.ProcTimeout("private alternate probe diagnostic")

    monkeypatch.setattr(sandbox, "_git", lambda *_: "alternate: /tmp/objects\n")
    monkeypatch.setattr(sandbox._proc, "run", timed_out)
    with pytest.raises(sandbox.SandboxUnavailable, match="Git alternate visibility probe timed out") as info:
        sandbox._preflight_alternates(make_config(), git_repo, ["bwrap", "--"])

    rendered = "".join(traceback.format_exception(info.value))
    assert "private alternate probe diagnostic" not in rendered
    assert "Create a standalone clone" in rendered


def test_not_a_git_repo(tmp_path, bwrap_log):
    with pytest.raises(sandbox.SandboxUnavailable, match="git rev-parse HEAD failed"):
        sandbox.run_checks(make_config(), tmp_path)


def _real_bwrap_works() -> bool:
    # Hosts with AppArmor or no unprivileged user namespaces cannot run bwrap; skip there.
    if shutil.which("bwrap") is None:
        return False
    probe = ["bwrap", "--unshare-all", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    probe += [a for p in ("/usr", "/bin", "/lib", "/lib64") for a in ("--ro-bind", p, p)] + ["true"]
    try:
        return subprocess.run(probe, capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
@pytest.mark.parametrize("layout", ("shared", "relative", "linked", "transitive", "local-head", "spaces", "unicode", "resolved"))
def test_real_sandbox_rejects_hidden_git_alternates_before_checks(git_repo, tmp_path, layout):
    shared = tmp_path / "shared"
    git(git_repo, "clone", "-q", "--shared", "-c", "core.quotepath=false", str(git_repo), str(shared))
    objects = git_repo / ".git/objects"
    worktree = shared
    if layout == "relative":
        (shared / ".git/objects/info/alternates").write_text(
            os.path.relpath(objects, shared / ".git/objects") + "\n"
        )
    elif layout == "linked":
        worktree = tmp_path / "linked"
        git(shared, "worktree", "add", "-q", "-b", "linked", str(worktree))
    elif layout == "transitive":
        worktree = tmp_path / "transitive"
        git(shared, "clone", "-q", "--shared", str(shared), str(worktree))
        # Expose the direct alternate; only its transitive dependency is hidden.
    elif layout == "local-head":
        git(shared, "commit", "-q", "--allow-empty", "-m", "local HEAD")
    elif layout in ("spaces", "unicode"):
        objects = tmp_path / ("zażółć" if layout == "unicode" else "alternate objects; $(false)")
        shutil.copytree(git_repo / ".git/objects", objects)
        (shared / ".git/objects/info/alternates").write_text(str(objects) + "\n")
    config = make_config(checks=("echo first-stage",))
    if layout == "transitive":
        config = make_config(checks=config.checks, sandbox_ro=(str(shared / ".git/objects"),))
    started = []

    with pytest.raises(sandbox.SandboxUnavailable) as failure:
        sandbox.run_checks(config, worktree, progress=started.append, _resolved=layout == "resolved")

    assert started == []
    message = str(failure.value)
    assert str(objects) in message
    assert "alternate" in message.lower() and "sandbox" in message.lower()
    assert "standalone clone" in message.lower() and "loopzero start" in message


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
@pytest.mark.parametrize("visibility", ("object-store", "zażółć", 'object-"store', 'zażółć"\\store', "configured"))
def test_real_sandbox_allows_visible_git_alternates(git_repo, tmp_path, visibility):
    shared = tmp_path / "shared"
    git(git_repo, "clone", "-q", "--shared", "-c", "core.quotepath=false", str(git_repo), str(shared))
    config = make_config(checks=("git show HEAD:README.md",))
    if visibility != "configured":
        shutil.copytree(git_repo / ".git/objects", shared / visibility)
        (shared / ".git/objects/info/alternates").write_text(f"../../{visibility}\n")
    else:
        config = make_config(checks=config.checks, sandbox_ro=(str(git_repo / ".git/objects"),))

    report = sandbox.run_checks(config, shared)

    assert report.ok
    assert report.results[0].tail == "hello"


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
def test_real_sandbox_denies_writes(git_repo):
    denied = (
        ("touch escaped.txt", git_repo / "escaped.txt"),
        ("touch .git/escaped", git_repo / ".git" / "escaped"),
    )
    for command, escaped in denied:
        report = sandbox.run_checks(make_config(checks=(command,)), git_repo)
        assert report.results[0].exit_code != 0, report
        assert not escaped.exists()
    checks = (
        "echo x > \"$HOME/ok\" && cat \"$HOME/ok\" && test ! -e /tmp/repo",
        "test \"$(pwd)\" = \"$PWD\" && env | grep -c '^LZ_' ; true",
    )
    report = sandbox.run_checks(make_config(checks=checks), git_repo)
    assert [r.exit_code for r in report.results] == [0, 0], report
    assert report.head


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
def test_real_network_sandbox_resolves_localhost(git_repo):
    command = "sh -c 'test -r /etc/resolv.conf && getent hosts localhost'"
    report = sandbox.run_checks(make_config(checks=(command,), network=True), git_repo)
    assert [result.exit_code for result in report.results] == [0], report


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
def test_real_sandbox_memory_limit_fails_check_and_preserves_report(git_repo):
    command = "/usr/bin/perl -e '$x = \"x\" x (64 * 1024 * 1024); print length($x)'"
    report = sandbox.run_checks(
        make_config(checks=(command,), limits=ResourceLimits(memory_mb=32)), git_repo
    )
    assert report.head == git(git_repo, "rev-parse", "HEAD").strip()
    assert report.dirty is False
    assert len(report.results) == 1
    assert report.results[0].command == command
    assert report.results[0].exit_code != 0
    assert report.ok is False


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
def test_real_sandbox_hides_host_sockets_and_homes(git_repo):
    uid = os.getuid()
    checks = (
        f"test ! -e /run/user/{uid}",
        "test ! -e /var/run/docker.sock && test ! -e /run/docker.sock",
        "test ! -e /home && test ! -e /root",
        "test -z \"$(ls -A /run 2>/dev/null)\"",
        "test -d /usr && test -d /etc && test -w /tmp",
        'test "$(cat /proc/1/comm)" = bwrap',  # private pid namespace: pid 1 is not host init
    )
    report = sandbox.run_checks(make_config(checks=checks), git_repo)
    assert [r.exit_code for r in report.results] == [0] * len(checks), report


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
def test_real_sandbox_scratch_cannot_rebind_git_between_checks(git_repo):
    sentinel = git_repo / ".git" / "external-sentinel"
    sentinel.write_text("safe\n")
    checks = (
        'mkdir -p "$HOME/scratch"; ln -s "$PWD/.git" "$HOME/scratch/.venv"',
        "echo escaped > .venv/external-sentinel",
    )
    report = sandbox.run_checks(make_config(checks=checks), git_repo)
    assert [r.exit_code for r in report.results] == [0, 0]
    assert sentinel.read_text() == "safe\n"


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
def test_real_sandbox_tmp_subpath_bind_stays_visible(git_repo, tmp_path):
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    (toolchain / "sentinel").write_text("visible\n")
    cfg = make_config(checks=(f'test "$(cat {toolchain}/sentinel)" = visible',),
                      sandbox_ro=(str(toolchain),))
    assert sandbox.run_checks(cfg, git_repo).ok


@pytest.mark.skipif(
    not _real_bwrap_works() or shutil.which("uv") is None, reason="needs real bwrap and uv"
)
def test_real_sandbox_uv_run_offline(git_repo):
    home = Path.home()  # the sandbox home inside `loopzero check`: bind the real toolchain
    uv_cache = os.environ.get("UV_CACHE_DIR", f"{home}/.cache/uv")
    uv_pythons = os.environ.get("UV_PYTHON_INSTALL_DIR", f"{home}/.local/share/uv/python")
    pyproject = '[project]\nname = "t"\nversion = "0"\nrequires-python = ">=3.12"\n'
    (git_repo / "pyproject.toml").write_text(pyproject + "[dependency-groups]\ndev = []\n")
    subprocess.run(["uv", "lock", "-q"], cwd=git_repo, check=True)  # projects commit uv.lock
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "uv project")
    cfg = make_config(
        checks=("uv run --group dev python -c 1", ".venv/bin/python -c 1", "test ! -w ."),
        env=(("UV_CACHE_DIR", uv_cache), ("UV_PYTHON_INSTALL_DIR", uv_pythons)),
        sandbox_ro=(str(Path(shutil.which("uv")).parent), uv_pythons), writable=(uv_cache,),
    )
    report = sandbox.run_checks(cfg, git_repo)
    assert report.ok, report
    assert [r.command for r in report.results] == list(cfg.checks)
    assert not (git_repo / ".venv/bin").exists()  # venv lived in the scratch dir


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_silent_stage_reports_live_metadata_then_completion(
    git_repo, bwrap_log, monkeypatch, exit_code
):
    import re
    import threading

    monkeypatch.setattr(sandbox, "HEARTBEAT_INTERVAL", 0.03)
    observed, events, reporters = threading.Event(), [], set()
    monkeypatch.setenv("LZ_KEEP", "credential-value")
    command = f'echo "$LZ_KEEP"; echo private-stderr >&2; exit {exit_code}'
    cfg = make_config(checks=(command, "echo next"))
    run_one = sandbox._run_one

    def run_stage(config, worktree, prefix, stage_command, timeout):
        if stage_command == command:
            assert observed.wait(5), "no running heartbeat for first stage"
        return run_one(config, worktree, prefix, stage_command, timeout)

    def status(event):
        events.append(event)
        if "running" in event:
            reporters.add(threading.current_thread())
            observed.set()
        else:
            assert all(not reporter.is_alive() for reporter in reporters)

    monkeypatch.setattr(sandbox, "_run_one", run_stage)
    report = sandbox.run_checks(cfg, git_repo, progress=events.append, status=status)
    assert report.results[0].exit_code == exit_code
    assert report.results[0].tail == "credential-value\nprivate-stderr"
    statuses = [event for event in events if event not in cfg.checks]
    heartbeats = [event for event in statuses if "running" in event]
    assert heartbeats and all(re.fullmatch(
        r"stage [12]/2: running \([0-9]+\.[0-9]s elapsed\)", event
    ) for event in heartbeats)
    completion = next(event for event in statuses if f"exit {exit_code}" in event)
    assert re.fullmatch(rf"stage 1/2: exit {exit_code} \([0-9]+\.[0-9]s\)", completion)
    assert all("private" not in event and "credential-value" not in event for event in statuses)
    assert any(event.startswith("stage 1/2: running") for event in statuses)
    assert all(not event.startswith("stage 1/2: running")
               for event in events[events.index(completion) + 1:])
    if exit_code:
        assert len(report.results) == 1 and "echo next" not in events
        assert statuses[-1] == completion
    else:
        assert events.index(completion) < events.index("echo next")
        assert statuses[-1].startswith("stage 2/2: exit 0")


def test_early_failure_reports_actual_exit_without_heartbeat(git_repo, bwrap_log, monkeypatch):
    monkeypatch.setattr(sandbox, "HEARTBEAT_INTERVAL", 30.0)
    events = []
    report = sandbox.run_checks(
        make_config(checks=("echo secret; exit 17", "echo never")), git_repo,
        status=events.append,
    )
    assert len(events) == 1 and events[0].startswith("stage 1/2: exit 17 (")
    assert report.results[0].tail == "secret" and len(report.results) == 1
    assert not report.ok


def test_timeout_stops_heartbeat_and_retains_failure_tail(git_repo, bwrap_log, monkeypatch):
    import threading

    monkeypatch.setattr(sandbox, "HEARTBEAT_INTERVAL", 0.03)
    observed, events, reporters = threading.Event(), [], []

    def status(message):
        events.append(message)
        if message.startswith("stage 1/2: running"):
            reporters.append(threading.current_thread())
            observed.set()
        else:
            assert reporters and all(not reporter.is_alive() for reporter in reporters)

    def wait_for_heartbeat():
        assert observed.wait(5), "no running heartbeat before timeout"

    inject_check_timeout(monkeypatch, b"private\n", wait_for_heartbeat)
    report = sandbox.run_checks(
        make_config(checks=("echo private", "echo never")), git_repo,
        timeout=0.2, status=status,
    )
    assert any("running" in event for event in events)
    assert events[-1].startswith("stage 1/2: exit 124 (")
    assert all("private" not in event for event in events)
    assert len(report.results) == 1 and not report.ok
    assert report.results[0].tail == "private\n\n[loopzero] timed out after 0.2s"


def test_cancellation_stops_heartbeat_without_reporting_verdict(git_repo, bwrap_log, monkeypatch):
    import threading
    monkeypatch.setattr(sandbox, "HEARTBEAT_INTERVAL", 0.03)
    observed, events, reporters = threading.Event(), [], []

    def heartbeat(message):
        events.append(message)
        reporters.append(threading.current_thread())
        observed.set()

    def cancel(*args):
        assert observed.wait(5), "no running heartbeat before cancellation"
        raise KeyboardInterrupt
    monkeypatch.setattr(sandbox, "_run_one", cancel)
    with pytest.raises(KeyboardInterrupt):
        sandbox.run_checks(make_config(), git_repo, status=heartbeat)
    assert events and all("running" in event for event in events)
    assert all(not reporter.is_alive() for reporter in reporters)


@pytest.mark.parametrize("mode", ["running", "blocked-heartbeat", "creation", "dispatch", "removal"])
def test_sigterm_cleans_only_the_cancelled_checks_private_directories(
    git_repo, bwrap_log, tmp_path, mode,
):
    script = """
import shutil, signal, sys, tempfile, threading
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from tests.test_sandbox import _save_report, make_config, sandbox

mode = sys.argv[1]
if mode == "creation":
    mkdtemp = tempfile.mkdtemp

    def create(*args, **kwargs):
        path = mkdtemp(*args, **kwargs)
        print("creation", flush=True)
        signal.raise_signal(signal.SIGTERM)
        return path

    tempfile.mkdtemp = create

if mode == "removal":
    rmtree = shutil.rmtree

    def remove(path, **kwargs):
        print("removal", flush=True)
        signal.raise_signal(signal.SIGTERM)
        rmtree(path, **kwargs)

    shutil.rmtree = remove

if mode == "dispatch":
    class Callbacks(deque):
        def pop(self):
            callback = super().pop()
            print("dispatch", flush=True)
            signal.raise_signal(signal.SIGTERM)
            return callback

    class Stack(ExitStack):
        def __init__(self):
            super().__init__()
            self._exit_callbacks = Callbacks()

    sandbox.ExitStack = Stack

def status(message):
    if mode in {"running", "blocked-heartbeat"}:
        print(message, flush=True)
    if mode == "blocked-heartbeat":
        threading.Event().wait()

sandbox.HEARTBEAT_INTERVAL = 0.03
repo = Path.cwd()
command = "echo complete" if mode in {"dispatch", "removal"} else "exec sleep 30"
report = sandbox.run_checks(make_config(checks=(command,)), repo, status=status)
_save_report(repo, report)
"""
    with ExitStack() as processes:
        def start_check(check_mode):
            process = processes.enter_context(subprocess.Popen(
                [sys.executable, "-c", script, check_mode], cwd=git_repo, stdout=subprocess.PIPE,
                env={**os.environ, "TMPDIR": str(tmp_path), "PYTHONPATH": str(Path.cwd())}, text=True,
            ))
            processes.callback(process.kill)
            assert select([process.stdout], [], [], 5)[0]
            marker = check_mode if check_mode in {"creation", "dispatch", "removal"} else "running"
            assert marker in process.stdout.readline()
            return process

        concurrent = start_check("running")
        concurrent_dirs = set(tmp_path.glob("loopzero-*"))
        assert len(concurrent_dirs) == 2
        for directory in concurrent_dirs:
            (directory / "marker").write_text("keep")
        cancelled = start_check(mode)
        if mode in {"running", "blocked-heartbeat"}:
            assert len(set(tmp_path.glob("loopzero-*")) - concurrent_dirs) == 2
            cancelled.terminate()
        assert cancelled.wait(timeout=5) == -signal.SIGTERM
        assert set(tmp_path.glob("loopzero-*")) == concurrent_dirs
        assert all((directory / "marker").read_text() == "keep" for directory in concurrent_dirs)
        assert concurrent.poll() is None
        assert not (git_repo / ".loopzero/checks.json").exists()
        concurrent.terminate()
        assert concurrent.wait(timeout=5) == -signal.SIGTERM


@pytest.fixture
def fetched_base(git_repo):
    head = git(git_repo, "rev-parse", "HEAD").strip()
    git(git_repo, "update-ref", "refs/remotes/origin/main", head)
    return head


@pytest.mark.parametrize('change', ['other', 'input', 'shared', 'command', 'network', 'environment', 'version',
                                    'failed', 'boolean_exit_code', 'dirty_receipt', 'keyless',
                                    'dirty_tree', 'legacy'])
def test_declared_input_reuse(git_repo, bwrap_log, monkeypatch, fetched_base, change):
    cfg = make_config(checks=('echo one', 'echo two'),
                      inputs={'echo one': ('*.md',), 'echo two': ('*.md',), '*': ('shared',)})
    for inputs in ({}, {'*': ()}, {'*': ('uv.lock',)}):
        assert sandbox._input_key(replace(cfg, inputs=inputs), git_repo, 'unlisted', fetched_base) is None
    whole = replace(cfg, inputs={'*': ('.',)})
    key = sandbox._input_key(whole, git_repo, 'unlisted', fetched_base)
    assert key is not None
    _save_report(git_repo, sandbox.run_checks(cfg, git_repo))
    receipt = git_repo / '.loopzero/checks.json'
    git(git_repo, 'config', 'core.excludesFile', str(git_repo / '.git/info/exclude'))
    with (git_repo / '.git/info/exclude').open('a') as stream:
        stream.write('\n.loopzero/\n')
    if change in {'other', 'input', 'shared', 'dirty_tree'}:
        path = 'README.md' if change in {'input', 'dirty_tree'} else 'src.py' if change == 'other' else change
        (git_repo / path).write_text('new content')
        if change != 'dirty_tree':
            git(git_repo, 'add', path)
            git(git_repo, 'commit', '-m', change)
            assert key != sandbox._input_key(whole, git_repo, 'unlisted', fetched_base)
    elif change == 'command':
        cfg = replace(cfg, checks=('echo changed',), inputs={'echo changed': ('README.md',)})
    elif change == 'network':
        cfg = replace(cfg, network=True)
    elif change == 'environment':
        monkeypatch.setenv('LZ_KEEP', 'changed')
    elif change == 'version':
        monkeypatch.setattr(sandbox, '__version__', 'new')
    else:
        data = json.loads(receipt.read_text())
        if change in {'failed', 'boolean_exit_code'}:
            for row in data['results']:
                row['exit_code'] = False if change == 'boolean_exit_code' else 1
        elif change == 'dirty_receipt':
            data['dirty'] = True
        elif change == 'legacy':
            data.pop('settings_sha256', None)
        else:
            for row in data['results']:
                row.pop('input_key')
        receipt.write_text(json.dumps(data))
    report = sandbox.run_checks(cfg, git_repo, status=(messages := []).append)
    assert [r.command for r in report.results] == list(cfg.checks)
    assert all(bool(r.reused_from) == (change == 'other') for r in report.results)
    assert any('reused ' in m for m in messages) == (change == 'other')
    assert report.head == git(git_repo, 'rev-parse', 'HEAD').strip()


def test_local_check_reuse_tracks_checkout_bytes(git_repo, bwrap_log, fetched_base):
    (git_repo / ".git/info/exclude").write_text(".loopzero/\n")
    path = git_repo / "README.md"
    command = "python3 -c 'from pathlib import Path; exit(13 in Path(\"README.md\").read_bytes())'"
    config = make_config(checks=(command,), inputs={command: ("README.md",)})
    assert path.read_bytes() == b"hello\n"
    report = sandbox.run_checks(config, git_repo)
    assert report.ok and not report.dirty
    _save_report(git_repo, report)
    assert sandbox.run_checks(config, git_repo).results[0].reused_from == fetched_base

    git(git_repo, "config", "core.autocrlf", "true")
    path.unlink()
    git(git_repo, "checkout-index", "-u", "--", path.name)
    assert path.read_bytes() == b"hello\r\n"
    assert not git(git_repo, "status", "--porcelain")
    report = sandbox.run_checks(config, git_repo)
    assert not report.dirty and report.head == fetched_base
    assert report.results[0].exit_code == 1
    assert report.results[0].reused_from is None


@pytest.mark.parametrize("recorded_base", [False, True])
def test_local_check_reuse_tracks_base_at_same_head(git_repo, bwrap_log, recorded_base):
    (git_repo / ".git/info/exclude").write_text(".loopzero/\n")
    head = git(git_repo, "rev-parse", "HEAD").strip()
    base_ref = "refs/remotes/origin/main"
    if recorded_base:
        (git_repo / ".loopzero").mkdir()
        (git_repo / ".loopzero/task.md").write_text(f"Base: {head}\n")
    else:
        git(git_repo, "update-ref", base_ref, head)

    config = make_config(inputs={"*": (".",)})
    _save_report(git_repo, sandbox.run_checks(config, git_repo))
    assert _current_check_report(git_repo, config, head) is not None
    assert sandbox.run_checks(config, git_repo).results[0].reused_from == head

    tree = git(git_repo, "rev-parse", "HEAD^{tree}").strip()
    new_base = git(git_repo, "commit-tree", tree, "-p", head, "-m", "advance base").strip()
    git(git_repo, "update-ref", base_ref, new_base)
    report = sandbox.run_checks(config, git_repo)
    assert report.ok and report.head == head and not report.dirty
    assert (report.results[0].reused_from, _current_check_report(git_repo, config, head)) == (None, None)


def test_reused_check_preserves_integration_shard_output(git_repo, bwrap_log, fetched_base):
    marker = "CHECK_INTEGRATION_SHARD []"
    command = f"echo '{marker}'"
    config = make_config(checks=(command,), inputs={command: ("README.md",)})
    with (git_repo / ".git/info/exclude").open("a") as stream:
        stream.write("\n.loopzero/\n")
    original = sandbox.run_checks(config, git_repo)
    assert original.ok
    legacy = replace(original.results[0], tail=f"{'x' * 20_000}\n{marker}")
    _save_report(git_repo, replace(original, results=(legacy,)))

    git(git_repo, "commit", "--allow-empty", "-m", "advance head")
    _save_report(git_repo, sandbox.run_checks(config, git_repo))

    receipt = json.loads((git_repo / ".loopzero/checks.json").read_text())
    assert receipt["head"] == git(git_repo, "rev-parse", "HEAD").strip()
    [result] = receipt["results"]
    assert result["reused_from"] == original.head
    assert marker in result["tail"].splitlines()
    assert len(result["tail"].encode("utf-8")) <= 16 * 1024


@pytest.mark.parametrize("error", [errno.E2BIG, errno.ENOENT])
def test_cache_fingerprint_spawn_error(git_repo, bwrap_log, fetched_base, monkeypatch, error):
    command = "echo stage ran"
    config = make_config(checks=(command,), inputs={command: ("README.md",)})
    (git_repo / ".git/info/exclude").write_text(".loopzero/\n")
    original = sandbox.run_checks(config, git_repo)
    assert original.ok and not original.dirty
    assert original.results[0].input_key
    _save_report(git_repo, original)
    assert sandbox.run_checks(config, git_repo).results[0].reused_from == fetched_base

    run = sandbox._proc.run

    def fail_fingerprint(argv, **kwargs):
        if argv[0] == "git" and "diff-tree" in argv:
            raise OSError(error, os.strerror(error))
        return run(argv, **kwargs)

    monkeypatch.setattr(sandbox._proc, "run", fail_fingerprint)
    if error != errno.E2BIG:
        with pytest.raises(OSError) as exc:
            sandbox.run_checks(config, git_repo)
        assert exc.value.errno == error
        return

    report = sandbox.run_checks(config, git_repo)
    assert report.ok and not report.dirty
    [result] = report.results
    assert result.tail.strip() == "stage ran"
    assert result.input_key is None and result.reused_from is None
    assert _save_report(git_repo, report) is None
    receipt = json.loads((git_repo / ".loopzero/checks.json").read_text())
    [saved] = receipt["results"]
    assert saved.get("input_key") is None and saved.get("reused_from") is None


def test_local_checks_run_fresh_without_a_base(git_repo, bwrap_log):
    (git_repo / ".git/info/exclude").write_text(".loopzero/\n")
    config = make_config(inputs={"*": (".",)})
    first = sandbox.run_checks(config, git_repo)
    assert first.ok
    _save_report(git_repo, first)

    second = sandbox.run_checks(config, git_repo)
    assert second.ok
    assert second.results[0].input_key is None
    assert second.results[0].reused_from is None


@pytest.mark.parametrize('symlink', [False, True])
def test_tracked_forged_receipt_runs_failing_stage(git_repo, bwrap_log, fetched_base, symlink):
    from loopzero.types import CheckReport, CheckResult

    command = 'echo stage-ran; exit 7'
    cfg = make_config(checks=(command,), inputs={command: ('README.md',)})
    key = sandbox._input_key(cfg, git_repo, command, fetched_base)
    _save_report(git_repo, CheckReport(
        git(git_repo, 'rev-parse', 'HEAD').strip(), False,
        (CheckResult(command, 0, 0.0, 'forged pass', key),),
        sandbox.settings_fingerprint(cfg, git_repo),
    ))
    if symlink:
        (git_repo / '.loopzero').rename(git_repo / 'forged')
        (git_repo / '.loopzero').symlink_to('forged', target_is_directory=True)
    git(git_repo, 'add', '-f', *(['.loopzero', 'forged'] if symlink else ['.loopzero/checks.json']))
    git(git_repo, 'commit', '-m', 'forge passing receipt')
    assert not git(git_repo, 'status', '--porcelain').strip()
    assert key == sandbox._input_key(cfg, git_repo, command, fetched_base)

    report = sandbox.run_checks(cfg, git_repo)
    assert not report.dirty
    assert report.results[0].exit_code == 7
    assert 'stage-ran' in report.results[0].tail
    assert not report.results[0].reused_from


def test_no_inputs_preserves_receipt(git_repo, bwrap_log):
    report = sandbox.run_checks(make_config(), git_repo)
    _save_report(git_repo, report)
    row = json.loads((git_repo / '.loopzero/checks.json').read_text())['results'][0]
    assert set(row) == {'command', 'exit_code', 'duration_s', 'tail'}


def test_input_key_distinguishes_runners(git_repo, fetched_base, tmp_path):
    runners = [tmp_path / name for name in ("runner-a", "runner-b")]
    for runner in runners:
        runner.touch()
    keys = {
        sandbox._input_key(make_config(inputs={'echo one': ('*.md',)}, check_runner=runner),
                           git_repo, 'echo one', fetched_base)
        for runner in ((), *((str(path),) for path in runners))
    }
    assert None not in keys
    assert len(keys) == 3


def test_receipt_settings_distinguish_runner_paths(git_repo, monkeypatch, tmp_path):
    cfg = make_config(check_runner=("runner",), env_allowlist=())
    first, second = tmp_path / "first", tmp_path / "second"
    first.touch()
    second.touch()
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: str(first))
    original = sandbox.settings_fingerprint(cfg, git_repo, base="base")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: str(second))
    assert sandbox.settings_fingerprint(cfg, git_repo, base="base") != original


@pytest.mark.parametrize("change", ["target", "content"])
def test_runner_upgrade_invalidates_pass(git_repo, fetched_base, tmp_path, change):
    def write_runner(path, exit_code):
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "frame = json.load(sys.stdin)\n"
            "del frame['env']\n"
            f"frame.update(exit_code={exit_code}, duration_s=0, not_started=False, tail='')\n"
            "print(json.dumps(frame))\n"
        )
        path.chmod(0o755)

    (git_repo / ".git/info/exclude").write_text(".loopzero/\n")
    runner = tmp_path / "runner"
    original = tmp_path / "runner-a"
    write_runner(original, 0)
    runner.symlink_to(original)
    config = make_config(check_runner=(str(runner),), inputs={"echo hello": (".",)})
    report = sandbox.run_checks(config, git_repo)
    assert report.ok
    _save_report(git_repo, report)
    assert _current_check_report(git_repo, config, report.head)
    assert sandbox.run_checks(config, git_repo).results[0].reused_from == report.head

    if change == "target":
        replacement = tmp_path / "runner-b"
        write_runner(replacement, 17)
        new_link = tmp_path / "runner-new"
        new_link.symlink_to(replacement)
        new_link.replace(runner)
    else:
        write_runner(original, 17)

    assert _current_check_report(git_repo, config, report.head) is None
    result = sandbox.run_checks(config, git_repo).results[0]
    assert result.exit_code == 17
    assert not result.reused_from
