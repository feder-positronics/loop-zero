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
from threading import Event, current_thread
from unittest.mock import MagicMock, Mock
from urllib.parse import quote

import pytest

from loopzero import cli, sandbox
from tests.conftest import bwrap_argv
from loopzero.cli import _current_check_report, _save_report
from loopzero.types import Config, ResourceLimits

from .conftest import commit_file, git

FAKE_BWRAP = '''#!/usr/bin/python3
import os
import sys

args = sys.argv[1:]
with open("{log}", "w", encoding="utf-8") as stream:
    stream.writelines(f"{{arg}}\\n" for arg in args)
separator = args.index("--")
options, command = args[:separator], args[separator + 1:]
env = {{}} if "--clearenv" in options else dict(os.environ)
if "--args" in options:
    index = options.index("--args")
    with os.fdopen(int(options[index + 1]), "rb") as arguments:
        options[index:index + 2] = list(map(os.fsdecode, arguments.read().split(b"\\0")[:-1]))
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
    for key in ("CODEX_HOME", "HOME"):
        if env.get(key) == "/tmp/home":
            env[key] = home_source
os.chdir(options[options.index("--chdir") + 1])
os.execvpe(command[0], command, env)
'''


def make_config(**overrides) -> Config:
    defaults = {"repo": "o/n", "base_branch": "main", "checks": ("echo hello",), "required_ci": (),
                "merge_strategy": "squash", "reviewers": ("claude",), "env_allowlist": ("PATH", "HOME", "LZ_KEEP")}
    return Config(**(defaults | overrides))


@pytest.mark.parametrize("inputs,expected", [
    ({}, "f7713e56ba051aaa822cf97799a51ec7c61f21488dd60f7a42b242a362589594"),
    ({"echo hello": (".",)}, "41de782373c01d9e87e55f2f37df3b246124c556097ac06d02556165ce000c66")])
def test_settings_fingerprint_is_stable_across_releases(inputs, expected):
    # This version deliberately rejects receipts created before stage redaction.
    config = make_config(env_allowlist=Config.env_allowlist, inputs=inputs)
    assert sandbox.settings_fingerprint(config) == expected


def test_fingerprints_bind_effective_allowlist(monkeypatch):
    monkeypatch.setenv("LZ_KEEP", "supplied")
    monkeypatch.delenv("LZ_UNSET", raising=False)
    config = make_config()
    reordered = make_config(env_allowlist=("LZ_KEEP", "HOME", "PATH", "LZ_KEEP", "LZ_UNSET"))
    assert sandbox.settings_fingerprint(reordered) == sandbox.settings_fingerprint(config)
    assert sandbox._execution_settings(reordered, "main") == sandbox._execution_settings(config, "main")
    monkeypatch.setenv("HEAVY_SLOT_GRANTED_CORES", "2")
    with_grant = make_config(env_allowlist=(*config.env_allowlist, "HEAVY_SLOT_GRANTED_CORES"))
    stage = sandbox._execution_settings(with_grant, "main")
    monkeypatch.setenv("HEAVY_SLOT_GRANTED_CORES", "4")
    assert sandbox._execution_settings(with_grant, "main") != stage


@pytest.fixture
def bwrap_log(fake_tool, tmp_path) -> Path:
    log = tmp_path / "bwrap.argv"
    fake_tool("bwrap", FAKE_BWRAP.format(log=log))
    return log


def test_report_head_and_results(git_repo, bwrap_log):
    checks = ("echo one", "echo two; exit 4", "echo never")
    report = sandbox.run_checks(make_config(checks=checks), git_repo)
    assert report.head == git(git_repo, "rev-parse", "HEAD").strip()
    assert report.dirty is False
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


SERVICE_KEY = 'p@ss/w0rd+1&"x\\9'


@pytest.mark.parametrize("command,exit_code,line_count,start,end", [
    ("for i in $(seq 1 50); do echo $i; done; echo err >&2", 0, 40, "12", "err"),
    ("""python3 -c 'print("x" * 20000 + " Ω end")'""", 0, 1, "x", " Ω end"),
    ("""python3 -c 'print("terminal line" + "\\n" * 20_000)'""", 0, 1, "terminal line", "terminal line"),
    ("./" * 8_200 + "absent/file", 1, 1, "", "otherwise restore the required file."),
    ('for i in $(seq 1 1000); do printf "%s----------" "$SERVICE_KEY"; done', 0, 1, "", "----------"),
    (f"printf %s {shlex.quote(json.dumps(SERVICE_KEY) + ' ' + quote(SERVICE_KEY, safe='') + ' ' + SERVICE_KEY)}", 0, 1,
     '"[REDACTED]"', "[REDACTED]"),
], ids=["lines", "oversized-line", "trailing-newlines", "missing-file", "split-key", "padded-and-encoded-key"])
def test_tail_merges_streams_and_truncates(git_repo, bwrap_log, monkeypatch, command, exit_code, line_count, start, end):
    monkeypatch.setenv("SERVICE_KEY", f" {SERVICE_KEY}\n" if end == "[REDACTED]" else SERVICE_KEY)
    (result,) = sandbox.run_checks(make_config(checks=(command,), env_allowlist=("PATH", "SERVICE_KEY")), git_repo).results
    if end == "----------":
        assert "[REDACTED]" in result.tail and not set(result.tail) - set("[REDACTED]-")
    elif end == "[REDACTED]":
        assert result.tail == '"[REDACTED]" [REDACTED] [REDACTED]'
    lines = result.tail.splitlines()
    assert (result.exit_code, len(lines)) == (exit_code, line_count)
    assert lines[0].startswith(start) and lines[-1].endswith(end)
    assert len(result.tail.encode("utf-8")) <= 16 * 1024


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
    [stage] = receipt["results"]
    assert stage["exit_code"] == stage_exit
    assert set(stage) == {"command", "exit_code", "duration_s", "tail"}
    assert capsys.readouterr().out.splitlines()[-1] == ("FAIL" if stage_exit else "PASS")


@pytest.mark.parametrize("limited", [False, True], ids=["default", "constrained"])
def test_bwrap_flags(git_repo, bwrap_log, monkeypatch, limited):
    options = {"limits": ResourceLimits(memory_mb=64, file_mb=3)} if limited else {}
    if limited:
        monkeypatch.setattr(resource, "getrlimit", lambda _: (0, 32 * 1024 * 1024))
    monkeypatch.setenv("LZ_KEEP", "kept")
    monkeypatch.setenv("LZ_DROP", "dropped")
    values = 'printf "%s\\n" "$PATH" "$LZ_KEEP" "${LZ_DROP-unset}" "$UV_CACHE_DIR" "$PYTHONDONTWRITEBYTECODE"'
    (result,) = sandbox.run_checks(make_config(checks=(values,), **options), git_repo).results
    argv = bwrap_argv(bwrap_log)
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
    assert "--args" in head and "--setenv" not in head and "kept" not in head
    assert result.tail.splitlines() == [os.environ["PATH"], "kept", "unset", "/tmp/uv-cache", "1"]
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
    limits = ["--as=33554432", "--fsize=3145728"] if limited else [
        f"--as={sandbox._cap_limit(4096 << 20, resource.RLIMIT_AS)}",
        f"--fsize={sandbox._cap_limit(2048 << 20, resource.RLIMIT_FSIZE)}",
    ]
    assert tail == ["prlimit", *limits, "--", "/bin/bash", "--posix", "-o", "pipefail", "-c", values]


@pytest.mark.parametrize("checks", [(), ("echo hello",)], ids=["probe", "stage"])
def test_network_binds_resolv_conf_after_run_tmpfs(git_repo, bwrap_log, checks):
    sandbox.run_checks(make_config(checks=checks, network=True), git_repo)
    argv = bwrap_argv(bwrap_log)
    if not checks:
        assert argv[argv.index("--") + 1 :] == ["true"]
    assert argv.index("--unshare-all") < argv.index("--share-net") < argv.index("--clearenv")
    resolv_conf = Path("/etc/resolv.conf")
    resolved = resolv_conf.resolve()
    if resolv_conf.is_symlink() and any(
        resolved.is_relative_to(root) for root in (Path("/tmp"), Path("/run"))
    ):
        source = destination = str(resolved.parent)
    else:
        source, destination = str(resolved), str(resolv_conf)
    bind_at = next(i for i, flag in enumerate(argv) if flag == "--ro-bind" and argv[i + 1:i + 3] == [source, destination])
    run_tmpfs_at = next(i for i, pair in enumerate(itertools.pairwise(argv)) if pair == ("--tmpfs", "/run"))
    assert bind_at > run_tmpfs_at


def test_extra_paths_and_env_override(git_repo, bwrap_log, tmp_path, monkeypatch):
    extra, cache = tmp_path / "ro", tmp_path / "rw"
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.setenv("RUFF_CACHE_DIR", "host-ruff")
    cfg = make_config(
        sandbox_ro=(str(extra),), writable=(str(cache),), scratch=(),
        env_allowlist=("PATH", "UV_CACHE_DIR", "RUFF_CACHE_DIR"), env=(("RUFF_CACHE_DIR", "cfg"),),
        checks=('printf "%s\\n" "$UV_CACHE_DIR" "$RUFF_CACHE_DIR"',),
    )
    (result,) = sandbox.run_checks(cfg, git_repo).results
    argv = bwrap_argv(bwrap_log)
    assert _pairs(argv, "--ro-bind-try") == [(str(extra),) * 2]
    assert _pairs(argv, "--bind-try") == [(str(cache),) * 2]
    assert len(_pairs(argv, "--bind")) == 1  # only HOME; no scratch
    assert result.tail.splitlines() == [str(cache), "cfg"]


def inject_check_timeout(monkeypatch, output, before_timeout=None):
    def read_output_tail(process, limit, timeout, _forms):
        if before_timeout is not None:
            before_timeout()
        raise subprocess.TimeoutExpired(process.args, timeout, output=output[-limit:])

    monkeypatch.setattr(sandbox._proc, "_read_output_tail", read_output_tail)


def test_verbose_stage_bounds_parent_memory(git_repo, bwrap_log):
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
        result = sandbox._run_one(make_config(), git_repo, ["bwrap", "--chdir", str(git_repo), "--"], command, 30)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.exit_code == 7
    assert result.tail == "last stdout\nfinal Ω diagnostic"
    assert peak - baseline < 4 * 1024 * 1024


def test_timeout_is_recorded_with_drained_output(git_repo, bwrap_log):
    command = "printf '%20000s\\n' ''; echo last diagnostic >&2; exec sleep 30"
    report = sandbox.run_checks(make_config(checks=(command, "echo after")), git_repo, timeout=1)
    (result,) = report.results
    assert result.exit_code == sandbox.TIMEOUT_EXIT and not report.ok
    assert result.tail.endswith("\nlast diagnostic\n\n[loopzero] timed out after 1s")
    assert len(result.tail.encode()) == 16 * 1024


def test_linked_worktree_binds_common_git_dir(git_repo, bwrap_log, tmp_path):
    linked = tmp_path / "linked"
    git(git_repo, "worktree", "add", "-q", str(linked), "-b", "feature")
    sandbox.run_checks(make_config(), linked)
    binds = _pairs(bwrap_argv(bwrap_log), "--ro-bind")
    common = (git_repo / ".git").resolve()
    assert binds[-2:] == [(str(linked.resolve()),) * 2, (str(common),) * 2]


@pytest.mark.parametrize("entry,diagnostic", [
    (".", "unsafe"), (".git", "Git metadata"), (".git/cache", "Git metadata"),
    ("linked/cache", "symlink component"), (None, "private check directories"),
    ("TMP/cache", "unsafe"), ("../cache", "unsafe"),
])
def test_scratch_rejects_protected_paths(git_repo, bwrap_log, tmp_path, monkeypatch, entry, diagnostic):
    if entry == "linked/cache":
        (git_repo / "linked").symlink_to(tmp_path, target_is_directory=True)
    if entry == "TMP/cache":
        entry = str(tmp_path / "cache")
    elif entry == "../cache":  # A refused-guard run must create paths only inside this test's directory.
        (tmp_path / "tmp").mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    config = make_config(writable=(tempfile.gettempdir(),)) if entry is None else make_config(scratch=(entry,))
    with pytest.raises(sandbox.SandboxUnavailable, match=diagnostic):
        sandbox.run_checks(config, git_repo)
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("denied", [False, True], ids=["missing", "denied"])
def test_bwrap_missing_or_probe_failure(git_repo, fake_bin, fake_tool, monkeypatch, denied):
    if denied:
        fake_tool("bwrap", "echo 'bwrap: No permissions to create new namespace' >&2\nexit 1\n")
    else:
        monkeypatch.setenv("PATH", str(fake_bin))
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        sandbox.run_checks(make_config(), git_repo)
    diagnosis = str(caught.value)
    if denied:
        assert "namespace creation failed" in diagnosis and "exit 1" in diagnosis
        assert "No permissions to create new namespace" in diagnosis
        assert "ask the host operator to allow user namespaces for bwrap" in diagnosis
    else:
        assert "bwrap binary missing" in diagnosis and "sudo apt install bubblewrap" in diagnosis


def test_probe_timeout_reports_recovery_without_private_diagnostic(git_repo, monkeypatch):
    monkeypatch.setattr(sandbox._proc, "run", Mock(side_effect=sandbox._proc.ProcTimeout("private probe diagnostic")))
    with pytest.raises(sandbox.SandboxUnavailable) as info:
        sandbox.probe(make_config(), git_repo, ["bwrap", "--"])

    assert str(info.value) == f"bwrap namespace creation timed out after {sandbox.GIT_TIMEOUT:g}s. Retry once, then ask the host operator to check bubblewrap if it still times out."
    assert info.value.__suppress_context__


def test_alternate_probe_skips_default_inventory_and_honors_override(git_repo, monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox, "_git", lambda *args: calls.append(args) or "alternate: /tmp/objects\n")
    monkeypatch.setattr(sandbox._proc, "run", Mock(side_effect=sandbox._proc.ProcTimeout("private alternate probe diagnostic")))
    cfg = make_config(env_allowlist=("GIT_ALTERNATE_OBJECT_DIRECTORIES",))
    monkeypatch.delenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", raising=False)
    prefix, common = ["bwrap", "--"], git_repo / ".git"
    sandbox._preflight_alternates(cfg, git_repo, prefix, common)
    assert not calls
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/objects")
    with pytest.raises(sandbox.SandboxUnavailable, match="Git alternate visibility probe timed out") as info:
        sandbox._preflight_alternates(cfg, git_repo, prefix, common)
    rendered = "".join(traceback.format_exception(info.value))
    assert "private alternate probe diagnostic" not in rendered
    assert "Transfer this task branch" in rendered


def test_not_a_git_repo(tmp_path, bwrap_log):
    with pytest.raises(sandbox.SandboxUnavailable, match="git rev-parse HEAD failed"):
        sandbox.run_checks(make_config(), tmp_path)


def test_unborn_branch_names_its_cause(tmp_path, bwrap_log):
    git(tmp_path, "init", "-q")
    with pytest.raises(sandbox.SandboxUnavailable, match="no commit yet; make the first commit"):
        sandbox.run_checks(make_config(), tmp_path)


def test_git_reason_prefers_the_fatal_line():
    assert sandbox._git_reason("fatal: bad thing\nusage: git x") == "fatal: bad thing"
    assert sandbox._git_reason("one\ntwo") == "two"


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


def interrupt_after_checkpoint(repo, config, *, verify_first_exit=False):
    def checkpoint(report):
        if verify_first_exit:
            assert report.results[0].exit_code == 0
        _save_report(repo, report)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        sandbox.run_checks(config, repo, checkpoint=checkpoint)


def test_checkpoint_drops_tails_from_pre_redaction_receipts(
    git_repo, bwrap_log, fetched_base, cache_check, receipt_path, monkeypatch,
):
    first, second = "echo first", "echo second"
    config = make_config(checks=(first, second), inputs={"*": (".",)})
    settings = sandbox._execution_settings
    with monkeypatch.context() as legacy:
        legacy.setattr(sandbox, "_execution_settings", lambda *args: settings(*args) | {"check_runner": []})
        report = cache_check(config, clean=True)
        assert all(stage.input_key for stage in report.results)
        stage = replace(report.results[1], tail="unsafe-fragment-from-old-tail")
        _save_report(git_repo, replace(report, results=(report.results[0], stage)))

    interrupt_after_checkpoint(git_repo, config, verify_first_exit=True)
    assert "unsafe-fragment-from-old-tail" not in receipt_path.read_text()


@pytest.mark.skipif(not _real_bwrap_works(), reason="real bwrap cannot create sandboxes here")
class TestRealSandbox:
    @pytest.mark.parametrize("interrupt", [False, True])
    def test_changed_consumer_input_refreshes_scratch(self, git_repo, fetched_base, cache_check, interrupt):
        commit_file(git_repo, "consumer.txt", "old\n", "Add consumer input")
        producer = "echo generated > .venv/generated"
        consumer = "test -s .venv/generated && cat consumer.txt"
        config = make_config(checks=(producer, consumer), scratch=(".venv",),
                             inputs={producer: ("README.md",), consumer: ("consumer.txt",)})
        cache_check(config, clean=True, verify_reuse=True)
        commit_file(git_repo, "consumer.txt", "new\n", "Update consumer input")

        if interrupt:
            interrupt_after_checkpoint(git_repo, config)

        report = sandbox.run_checks(config, git_repo)
        assert report.ok
        assert report.results[0].reused_from is None
        assert report.results[1].tail.strip() == "new"

    @pytest.mark.parametrize("layout", ("shared", "relative", "linked", "transitive", "local-head", "spaces", "unicode"))
    def test_real_sandbox_rejects_hidden_git_alternates_before_checks(self, git_repo, tmp_path, layout):
        shared = tmp_path / "shared"
        git(git_repo, "clone", "-q", "--shared", "-c", "core.quotepath=false", str(git_repo), str(shared))
        objects = git_repo / ".git/objects"
        worktree = shared
        if layout == "relative":
            (shared / ".git/objects/info/alternates").write_text(os.path.relpath(objects, shared / ".git/objects") + "\n")
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
        visible_stores = (str(shared / ".git/objects"),) if layout == "transitive" else ()
        config = make_config(checks=("echo first-stage",), sandbox_ro=visible_stores)
        started = []

        with pytest.raises(sandbox.SandboxUnavailable) as failure:
            sandbox.run_checks(config, worktree, progress=started.append)

        assert started == []
        message = str(failure.value)
        assert str(objects) in message
        assert "alternate" in message.lower() and "sandbox" in message.lower()
        assert "transfer this task branch" in message.lower() and "standalone clone" in message.lower()
        assert "rerun loopzero check at the same commit" in message.lower()

    @pytest.mark.parametrize("visibility", ("object-store", "zażółć", 'object-"store', 'zażółć"\\store', "configured"))
    def test_real_sandbox_allows_visible_git_alternates(self, git_repo, tmp_path, visibility):
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

    def test_real_sandbox_denies_writes(self, git_repo):
        denied = (
            ("touch escaped.txt", git_repo / "escaped.txt"),
            ("touch .git/escaped", git_repo / ".git" / "escaped"),
        )
        for command, escaped in denied:
            report = sandbox.run_checks(make_config(checks=(command,)), git_repo)
            assert report.results[0].exit_code != 0, report
            assert not escaped.exists()
        checks = ('echo x > "$HOME/ok" && cat "$HOME/ok" && test ! -e /tmp/repo',
                  'test "$(pwd)" = "$PWD" && env | grep -c "^LZ_" ; true')
        report = sandbox.run_checks(make_config(checks=checks), git_repo)
        assert [r.exit_code for r in report.results] == [0, 0], report

    def test_real_sandbox_memory_limit_fails_check_and_preserves_report(self, git_repo):
        command = "/usr/bin/perl -e '$x = \"x\" x (64 * 1024 * 1024); print length($x)'"
        report = sandbox.run_checks(
            make_config(checks=(command,), limits=ResourceLimits(memory_mb=32)), git_repo
        )
        [result] = report.results
        assert result.command == command and result.exit_code != 0
        assert not report.ok

    @pytest.mark.parametrize("network", [False, True], ids=["isolated", "localhost"])
    def test_real_sandbox_system_visibility(self, git_repo, network):
        checks = ("test -r /etc/resolv.conf && getent hosts localhost",) if network else (
            f"test ! -e /run/user/{os.getuid()}",
            "test ! -e /var/run/docker.sock && test ! -e /run/docker.sock",
            "test ! -e /home && test ! -e /root",
            "test -z \"$(ls -A /run 2>/dev/null)\"",
            "test -d /usr && test -d /etc && test -w /tmp",
            'test "$(cat /proc/1/comm)" = bwrap',  # private pid namespace: pid 1 is not host init
        )
        report = sandbox.run_checks(make_config(checks=checks, network=network), git_repo)
        if network:
            assert report.ok
        else:
            assert [r.exit_code for r in report.results] == [0] * len(checks), report

    def test_real_sandbox_scratch_cannot_rebind_git_between_checks(self, git_repo):
        sentinel = git_repo / ".git" / "external-sentinel"
        sentinel.write_text("safe\n")
        checks = (
            'mkdir -p "$HOME/scratch"; ln -s "$PWD/.git" "$HOME/scratch/.venv"',
            "echo escaped > .venv/external-sentinel",
        )
        report = sandbox.run_checks(make_config(checks=checks), git_repo)
        assert [r.exit_code for r in report.results] == [0, 0]
        assert sentinel.read_text() == "safe\n"

    def test_real_sandbox_tmp_subpath_bind_stays_visible(self, git_repo, tmp_path):
        toolchain = tmp_path / "toolchain"
        toolchain.mkdir()
        (toolchain / "sentinel").write_text("visible\n")
        command = f'test "$(cat {shlex.quote(str(toolchain / "sentinel"))})" = visible'
        cfg = make_config(checks=(command,), sandbox_ro=(str(toolchain),))
        assert sandbox.run_checks(cfg, git_repo).ok

    @pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
    @pytest.mark.parametrize("interrupt", [False, True])
    def test_real_sandbox_uv_run_offline(self, git_repo, tmp_path, fetched_base, receipt_path, interrupt):
        uv_cache = tmp_path / "uv-cache"
        uv_cache.mkdir()
        uv = shutil.which("uv")
        assert uv is not None
        env = {
            "UV_CACHE_DIR": str(uv_cache),
            "UV_NO_CONFIG": "1",
            "UV_OFFLINE": "1",
            "UV_PYTHON": str(Path(sys._base_executable).resolve()),
            "UV_PYTHON_DOWNLOADS": "never",
        }
        pyproject = '[project]\nname = "t"\nversion = "0"\nrequires-python = ">=3.12"\n'
        (git_repo / "pyproject.toml").write_text(pyproject + "[dependency-groups]\ndev = []\n")
        lock_env = {"HOME": str(tmp_path), "PATH": os.defpath, **env}
        subprocess.run([uv, "lock", "-q"], cwd=git_repo, env=lock_env, check=True)
        git(git_repo, "add", "-A")
        git(git_repo, "commit", "-q", "-m", "uv project")
        cfg = make_config(
            checks=(".venv/bin/python -B -c 1", "uv run --group dev python -c 1", "test ! -w ."),
            inputs={"*": (".",)},
            env=tuple(env.items()),
            sandbox_ro=(str(Path(uv).parent), sys.base_prefix),
            writable=(str(uv_cache),),
        )
        if interrupt:
            interrupt_after_checkpoint(git_repo, cfg, verify_first_exit=True)

        report = sandbox.run_checks(cfg, git_repo)
        assert report.ok, report
        assert [r.command for r in report.results] == list(cfg.checks)
        if interrupt:
            assert report.results[0].reused_from == report.head
        assert not (git_repo / ".venv/bin").exists()  # venv lived in the scratch dir


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


@pytest.fixture
def heartbeat_log(monkeypatch):
    monkeypatch.setattr(sandbox, "FIRST_HEARTBEAT_DELAY", 0.03)
    observed, events, reporters = Event(), [], set()

    def status(message):
        events.append(message)
        if ": running " in message:
            reporters.add(current_thread())
            observed.set()
        else:
            assert reporters and all(not reporter.is_alive() for reporter in reporters)

    def wait():
        assert observed.wait(5), "no running heartbeat"

    return events, reporters, status, wait


@pytest.mark.parametrize("exit_code", [0, 7])
def test_silent_stage_reports_live_metadata_then_completion(
    git_repo, bwrap_log, monkeypatch, heartbeat_log, exit_code
):
    import re

    events, _, status, wait_for_heartbeat = heartbeat_log
    monkeypatch.setenv("LZ_KEEP", "credential-value")
    monkeypatch.setenv("AUDIT_TOKEN", "p1n-secret")
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("RUST_BACKTRACE", "1")
    command = ('test -z "$LZ_KEEP" || echo "$LZ_KEEP"; echo "$AUDIT_TOKEN"; '
               'echo "tests/test_example.py:12: $RUST_BACKTRACE $NODE_ENV $DJANGO_DEBUG $CHECK_PYTHON $AUTH_ENABLED" >&2; '
               f'exit {exit_code}')
    cfg = make_config(checks=(command, "echo next"), env_allowlist=("PATH", "AUDIT_TOKEN", "AUTH_ENABLED", "RUST_BACKTRACE"),
                      env=(("NODE_ENV", "test"), ("DJANGO_DEBUG", "true"), ("CHECK_PYTHON", "/opt/cpython-3.14/bin/python3.14")))
    run_one = sandbox._run_one

    def run_stage(config, worktree, prefix, stage_command, timeout):
        if stage_command == command:
            wait_for_heartbeat()
        return run_one(config, worktree, prefix, stage_command, timeout)

    monkeypatch.setattr(sandbox, "_run_one", run_stage)
    report = sandbox.run_checks(cfg, git_repo, progress=events.append, status=status)
    assert report.results[0].exit_code == exit_code
    assert report.results[0].tail == "[REDACTED]\ntests/test_example.py:12: 1 test true /opt/cpython-3.14/bin/python3.14 1"
    statuses = [event for event in events if event not in cfg.checks]
    heartbeats = [event for event in statuses if "running" in event]
    assert heartbeats and all(re.fullmatch(
        r"stage [12]/2: running \([0-9]+\.[0-9]s elapsed\)", event
    ) for event in heartbeats)
    completion = next(event for event in statuses if f"exit {exit_code}" in event)
    assert re.fullmatch(rf"stage 1/2: exit {exit_code} \([0-9]+\.[0-9]s\)", completion)
    assert all("tests/test_example.py" not in event and "p1n" not in event for event in statuses)
    assert any(event.startswith("stage 1/2: running") for event in statuses)
    assert all(not event.startswith("stage 1/2: running")
               for event in events[events.index(completion) + 1:])
    if exit_code:
        assert len(report.results) == 1 and "echo next" not in events
        assert statuses[-1] == completion
    else:
        assert events.index(completion) < events.index("echo next")
        assert statuses[-1].startswith("stage 2/2: exit 0")


@pytest.mark.parametrize("timed_out", [False, True], ids=["early-failure", "timeout"])
def test_failed_stage_status_and_tail(git_repo, bwrap_log, monkeypatch, request, timed_out):
    if timed_out:
        events, _, status, wait = request.getfixturevalue("heartbeat_log")
        inject_check_timeout(monkeypatch, b"private\n", wait)
        command, exit_code, tail = "echo private", 124, "private\n\n[loopzero] timed out after 0.2s"
    else:
        events = []
        status = events.append
        command, exit_code, tail = "echo secret; exit 17", 17, "secret"
    report = sandbox.run_checks(make_config(checks=(command, "echo never")), git_repo,
                                status=status, **({"timeout": 0.2} if timed_out else {}))
    assert len(report.results) == 1 and not report.ok
    assert report.results[0].tail == tail
    assert events[-1].startswith(f"stage 1/2: exit {exit_code} (")
    if timed_out:
        assert any("running" in event for event in events)
        assert all("private" not in event for event in events)
    else:
        assert len(events) == 1


def test_cancellation_stops_heartbeat_without_reporting_verdict(git_repo, bwrap_log, monkeypatch, heartbeat_log):
    events, reporters, status, wait_for_heartbeat = heartbeat_log
    def cancel(*args):
        wait_for_heartbeat()
        raise KeyboardInterrupt
    monkeypatch.setattr(sandbox, "_run_one", cancel)
    with pytest.raises(KeyboardInterrupt):
        sandbox.run_checks(make_config(), git_repo, status=status)
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
def interrupt():
    print(mode, flush=True)
    signal.raise_signal(signal.SIGTERM)

def interrupt_after(call):
    def wrapped(*args, **kwargs):
        result = call(*args, **kwargs)
        interrupt()
        return result
    return wrapped

if mode == "creation":
    tempfile.mkdtemp = interrupt_after(tempfile.mkdtemp)
elif mode == "removal":
    rmtree = shutil.rmtree
    def remove(path, **kwargs):
        interrupt()
        rmtree(path, **kwargs)
    shutil.rmtree = remove
elif mode == "dispatch":
    class Callbacks(deque):
        pop = interrupt_after(deque.pop)

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

sandbox.FIRST_HEARTBEAT_DELAY = 0.03
repo = Path.cwd()
command = "echo complete" if mode in {"dispatch", "removal"} else "exec sleep 30"
report = sandbox.run_checks(make_config(checks=(command,)), repo, status=status)
_save_report(repo, report)
"""
    with ExitStack() as processes:
        def start_check(check_mode):
            process = processes.enter_context(subprocess.Popen(
                [sys.executable, "-c", script, check_mode], cwd=git_repo, stdout=subprocess.PIPE,
                env={**os.environ, "TMPDIR": str(tmp_path), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}, text=True,
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


@pytest.fixture
def receipt_path(git_repo):
    with (git_repo / ".git/info/exclude").open("a") as stream:
        stream.write("\n.loopzero/\n")
    return git_repo / ".loopzero/checks.json"


@pytest.fixture
def cache_check(git_repo, receipt_path):
    def cache(config, *, clean=False, verify_reuse=False):
        report = sandbox.run_checks(config, git_repo)
        assert report.ok
        if clean:
            assert not report.dirty
        _save_report(git_repo, report)
        if verify_reuse:
            assert sandbox.run_checks(config, git_repo).results[0].reused_from == report.head
        return report

    return cache


@pytest.mark.parametrize("middle", [(), ("*.nomatch",)])
@pytest.mark.parametrize("scratch", [True, False])
def test_reuse_skipped_before_unkeyed_stage(git_repo, bwrap_log, fetched_base, cache_check, scratch, middle):
    inputs = {"echo one": ("*.md",), "echo three": ("*.md",), **({"echo two": middle} if middle else {})}
    cfg = make_config(checks=("echo one", "echo two", "echo three"), scratch=(".venv",) if scratch else (),
                      inputs=inputs)
    cache_check(cfg)
    reused = [bool(r.reused_from) for r in sandbox.run_checks(cfg, git_repo).results]
    assert reused == ([False, False, True] if scratch else [True, False, True])


def test_earlier_stage_failure_precedes_later_input_error(
    git_repo, bwrap_log, fetched_base, cache_check, monkeypatch,
):
    first = "grep -qx ready README.md || { echo 'earlier failure'; exit 7; }"
    later = "echo later"
    commit_file(git_repo, "README.md", "ready\n", "Set up passing check")
    commit_file(git_repo, "later.txt", "later\n", "Add later input")
    config = make_config(checks=(first, later), scratch=(".venv",),
                         inputs={first: ("README.md",), later: ("later.txt",)})
    cache_check(config, clean=True, verify_reuse=True)
    commit_file(git_repo, "README.md", "broken\n", "Make earlier check fail")
    input_blobs = sandbox._input_blobs

    def lookup(config, worktree, paths):
        if paths == ("later.txt",):
            raise OSError("later lookup failed")
        return input_blobs(config, worktree, paths)

    monkeypatch.setattr(sandbox, "_input_blobs", lookup)
    [result] = sandbox.run_checks(config, git_repo).results
    assert (result.exit_code, result.tail.strip()) == (7, "earlier failure")


@pytest.mark.parametrize('change', ['other', 'input', 'shared', 'command', 'network', 'environment', 'version',
                                    'failed', 'boolean_exit_code', 'dirty_receipt', 'keyless',
                                    'dirty_tree', 'legacy', 'grant'])
def test_declared_input_reuse(git_repo, bwrap_log, monkeypatch, fetched_base, change, cache_check, receipt_path):
    monkeypatch.setenv('AUDIT_TOKEN', 'fake-cached-token')
    cfg = make_config(checks=('echo one', 'echo two'), env=(('NODE_ENV', 'test'),), env_allowlist=('PATH', 'HOME', 'LZ_KEEP', 'AUDIT_TOKEN'),
                      inputs={'echo one': ('*.md',), 'echo two': ('*.md',), '*': ('shared',)})
    if change == 'grant':
        cfg = replace(cfg, env_allowlist=(*cfg.env_allowlist, "HEAVY_SLOT_GRANTED_CORES"))
        monkeypatch.setenv("HEAVY_SLOT_GRANTED_CORES", "2")
    for inputs in ({}, {'*': ()}, {'*': ('uv.lock',)}):
        assert sandbox._input_key(replace(cfg, inputs=inputs), git_repo, 'unlisted', fetched_base) is None
    whole = replace(cfg, inputs={'*': ('.',)})
    key = sandbox._input_key(whole, git_repo, 'unlisted', fetched_base)
    assert key is not None
    first = cache_check(cfg, clean=True)
    original_tails = tuple(replace(r, tail=f"{r.tail} tests/test_example.py:12 fake-cached-token") for r in first.results)
    _save_report(git_repo, replace(first, results=original_tails))
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
        assert _current_check_report(git_repo, cfg, fetched_base) is None
    elif change == 'grant':
        monkeypatch.setenv("HEAVY_SLOT_GRANTED_CORES", "4")
        assert _current_check_report(git_repo, cfg, fetched_base) is not None
        monkeypatch.delenv("HEAVY_SLOT_GRANTED_CORES")
        assert _current_check_report(git_repo, cfg, fetched_base) is not None
    elif change == 'version':
        dist = Mock(version="2.0.0a0")
        monkeypatch.setattr("importlib.metadata.distribution", lambda _name: dist)
        dist.read_text.return_value = json.dumps({"vcs_info": {"commit_id": "a" * 40}})
        cache_check(cfg, clean=True)
        dist.read_text.return_value = json.dumps({"vcs_info": {"commit_id": "b" * 40}})
    else:
        data = json.loads(receipt_path.read_text())
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
        receipt_path.write_text(json.dumps(data))
    report = sandbox.run_checks(cfg, git_repo, status=(messages := []).append)
    assert [r.command for r in report.results] == list(cfg.checks)
    assert all(bool(r.reused_from) == (change == 'other') for r in report.results)
    if change == 'other':
        assert [r.tail for r in report.results] == [
            'one tests/test_example.py:12 [REDACTED]', 'two tests/test_example.py:12 [REDACTED]',
        ]
    assert any('reused ' in m for m in messages) == (change == 'other')
    assert report.head == git(git_repo, 'rev-parse', 'HEAD').strip()


@pytest.mark.parametrize("swap", ["committed_symlink", "symlinked_directory", "fifo"])
def test_input_key_reads_only_committed_regular_files_in_place(git_repo, fetched_base, monkeypatch, swap):
    command = "true"
    config = make_config(checks=(command,), inputs={command: ("inputs",)})
    value = git_repo / "inputs/value"
    value.parent.mkdir()
    value.write_text("v\n")
    git(git_repo, "add", "inputs")
    git(git_repo, "commit", "-qm", "inputs")
    assert sandbox._input_key(config, git_repo, command, fetched_base)
    if swap == "committed_symlink":
        value.unlink()
        value.symlink_to("elsewhere")
        git(git_repo, "commit", "-qam", "link")
        value.unlink()
        value.write_text("v\n")
    elif swap == "symlinked_directory":
        value.parent.rename(git_repo / "real")
        value.parent.symlink_to("real", target_is_directory=True)
    else:
        value.unlink()
        os.mkfifo(value)
        fifo, real_open = str(value.resolve()), Path.open

        def never_the_fifo(path, *args, **kwargs):
            if str(path) == fifo:
                pytest.fail("hashing opened a FIFO, which blocks until a writer appears")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", never_the_fifo)
    assert sandbox._input_key(config, git_repo, command, fetched_base) is None


@pytest.mark.parametrize("case", ["typo", "empty", "dirty", "no_base"])
def test_unmatched_inputs_run_fresh(git_repo, bwrap_log, fetched_base, case, cache_check):
    command = 'test "$(cat README.md)" = hello'
    paths = () if case == "empty" else ("ipnut.txt",)
    config = make_config(checks=(command,), inputs={command: paths})
    cache_check(config)
    (git_repo / "README.md").write_text("fail\n")
    if case != "dirty":
        git(git_repo, "commit", "-qam", "change actual input")
    if case == "no_base":
        git(git_repo, "update-ref", "-d", "refs/remotes/origin/main")

    result = sandbox.run_checks(config, git_repo, status=(messages := []).append)
    assert result.results[0].exit_code == 1
    assert any("workflow.toml: checks.inputs" in message and repr(paths) in message
               for message in messages)


@pytest.mark.parametrize("path", [":(bogus)README.md", "README\0.md", "README.md"])
def test_input_diagnostic_failure_does_not_block_dirty_check(
    git_repo, bwrap_log, fetched_base, monkeypatch, path,
):
    if path == "README.md":
        monkeypatch.setattr(sandbox, "_input_blobs", Mock(side_effect=sandbox._proc.ProcTimeout("input lookup timed out")))
    (git_repo / "README.md").write_text("dirty\n")
    command = 'test "$(cat README.md)" = dirty'
    config = make_config(checks=(command,), inputs={command: (path,)})
    report = sandbox.run_checks(config, git_repo, status=lambda _: None)
    assert report.ok and report.dirty and report.results[0].command == command


def test_local_check_reuse_tracks_checkout_bytes(git_repo, bwrap_log, fetched_base, cache_check):
    path = git_repo / "README.md"
    command = "python3 -c 'from pathlib import Path; exit(13 in Path(\"README.md\").read_bytes())'"
    config = make_config(checks=(command,), inputs={command: ("README.md",)})
    assert path.read_bytes() == b"hello\n"
    assert cache_check(config, clean=True, verify_reuse=True).head == fetched_base

    git(git_repo, "config", "core.autocrlf", "true")
    path.unlink()
    git(git_repo, "checkout-index", "-u", "--", path.name)
    assert path.read_bytes() == b"hello\r\n"
    assert not git(git_repo, "status", "--porcelain")
    report = sandbox.run_checks(config, git_repo)
    assert not report.dirty and report.head == fetched_base
    assert report.results[0].exit_code == 1
    assert report.results[0].reused_from is None


@pytest.mark.parametrize("base", ["tracked", "recorded", "absent"])
def test_local_check_reuse_tracks_base_at_same_head(git_repo, bwrap_log, base, cache_check):
    head = git(git_repo, "rev-parse", "HEAD").strip()
    base_ref = "refs/remotes/origin/main"
    if base == "recorded":
        (git_repo / ".loopzero").mkdir()
        (git_repo / ".loopzero/task.md").write_text(f"## Base\nmain @ {head}\n")
    elif base == "tracked":
        git(git_repo, "update-ref", base_ref, head)

    config = make_config(inputs={"*": (".",)})
    cache_check(config, clean=True, verify_reuse=base != "absent")
    if base == "absent":
        [result] = sandbox.run_checks(config, git_repo).results
        assert (result.exit_code, result.input_key, result.reused_from) == (0, None, None)
        return

    tree = git(git_repo, "rev-parse", "HEAD^{tree}").strip()
    new_base = git(git_repo, "commit-tree", tree, "-p", head, "-m", "advance base").strip()
    git(git_repo, "update-ref", base_ref, new_base)
    report = sandbox.run_checks(config, git_repo)
    assert report.results[0].reused_from is None and _current_check_report(git_repo, config, head) is not None


def test_reused_check_preserves_integration_shard_output(git_repo, bwrap_log, fetched_base, receipt_path):
    marker = "CHECK_INTEGRATION_SHARD []"
    command = f"echo '{marker}'"
    config = make_config(checks=(command,), inputs={command: ("README.md",)})
    original = sandbox.run_checks(config, git_repo)
    legacy = replace(original.results[0], tail=f"{'x' * 20_000}\n{marker}")
    _save_report(git_repo, replace(original, results=(legacy,)))

    git(git_repo, "commit", "--allow-empty", "-m", "advance head")
    _save_report(git_repo, sandbox.run_checks(config, git_repo))

    receipt = json.loads(receipt_path.read_text())
    assert receipt["head"] == git(git_repo, "rev-parse", "HEAD").strip()
    [result] = receipt["results"]
    assert original.ok and result["reused_from"] == original.head
    assert marker in result["tail"].splitlines()
    assert len(result["tail"].encode("utf-8")) <= 16 * 1024


def test_input_blobs_keep_git_output_byte_for_byte(git_repo):
    # Input keys hash diff-tree output; stripping would invalidate saved receipts.
    empty = git(git_repo, "hash-object", "-t", "tree", "/dev/null").strip()
    raw = git(git_repo, "-c", "core.quotePath=true", "diff-tree", "--no-commit-id",
              "--raw", "-r", "--no-abbrev", empty, "HEAD", "--", "README.md")
    assert raw.endswith("\n") and sandbox._input_blobs(make_config(), git_repo, ("README.md",)) == raw


@pytest.mark.parametrize("error", [errno.E2BIG, errno.ENOENT])
def test_cache_fingerprint_spawn_error(git_repo, bwrap_log, fetched_base, monkeypatch, error, cache_check):
    command = "echo stage ran"
    config = make_config(checks=(command,), inputs={command: ("README.md",)})
    original = cache_check(config, clean=True, verify_reuse=True)
    assert original.results[0].input_key and original.head == fetched_base

    run = sandbox._proc.run

    def fail_fingerprint(argv, **kwargs):
        if argv[0] == "git" and "diff-tree" in argv:
            raise OSError(error, os.strerror(error))
        return run(argv, **kwargs)

    monkeypatch.setattr("loopzero.worktree.run", fail_fingerprint)
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


@pytest.mark.parametrize('symlink', [False, True])
def test_tracked_forged_receipt_runs_failing_stage(git_repo, bwrap_log, fetched_base, symlink):
    from loopzero.types import CheckReport, CheckResult

    command = 'echo stage-ran; exit 7'
    cfg = make_config(checks=(command,), inputs={command: ('README.md',)})
    key = sandbox._input_key(cfg, git_repo, command, fetched_base)
    _save_report(git_repo, CheckReport(
        git(git_repo, 'rev-parse', 'HEAD').strip(), False,
        (CheckResult(command, 0, 0.0, 'forged pass', key),),
        sandbox.settings_fingerprint(cfg),
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


@pytest.mark.parametrize("timed_out", [False, True])
@pytest.mark.parametrize(("output", "canary"), [
    ('print("ghp_" + "q" * 40_000)', "q" * 10),
    ('print("ghp_" + "q" * 180_000)', "q" * 10),
    ('print("https://ghp_FAKE0123456789:example-password@host")', "example-password"),
    ('print(os.environ["MULTILINE_SECRET"])', "private-second-line"),
    ('print("-----BEGIN " + "A" * 180_000)', "A" * 10),
    ('''
print("-----BEGIN PRIVATE KEY-----")
for _ in range(4_500):
    print("synthetic-private-material")
print("-----END PRIVATE KEY-----")
''', "synthetic-private-material"),
])
def test_check_output_redaction_across_capture_boundaries(git_repo, bwrap_log, monkeypatch, timed_out, output, canary):
    monkeypatch.setenv("MULTILINE_SECRET", "private-first-line\r\nprivate-second-line")
    code = f"""
import os
import sys
import time

{output}
print("ordinary diagnostic", flush=True)
if sys.argv[1] == "True":
    time.sleep(30)
"""
    command = shlex.join([sys.executable, "-c", code, str(timed_out)])
    config = make_config(env_allowlist=("PATH", "MULTILINE_SECRET"))
    result = sandbox._run_one(config, git_repo, ["bwrap", "--chdir", str(git_repo), "--"], command, 2)
    assert result.exit_code == (sandbox.TIMEOUT_EXIT if timed_out else 0)
    assert "ordinary diagnostic" in result.tail
    assert ("timed out" in result.tail) == timed_out
    assert canary not in result.tail


def test_capture_reassembles_key_opener_when_cut_splits_the_prefix(monkeypatch):
    proc = sandbox._proc
    limit = sandbox.CHECK_TAIL_BYTES
    first = b"-----BEGIN ".ljust(limit + 64 * 1024 + 1, b"A")
    chunks = iter((first[:65536], first[65536:],
                   b" PRIVATE KEY-----\nprivate-body\n-----END PRIVATE KEY-----\nordinary diagnostic\n", b""))
    with monkeypatch.context() as patch:
        patch.setattr(proc.os, "read", lambda *_args: next(chunks))
        patch.setattr(proc.selectors, "DefaultSelector", MagicMock())
        tail = proc._read_output_tail(Mock(stdout=Mock(encoding="utf-8")), limit, 10, ())
    assert tail.strip() == "ordinary diagnostic"


@pytest.mark.parametrize(("skip", "expected"), [(0, "[REDACTED] end"), (1, " end")])
def test_redact_overlapping_occurrences_of_one_forwarded_value(skip, expected):
    assert sandbox._proc.redact("abcabcabc end", ("abcabc",), skip=skip) == expected
