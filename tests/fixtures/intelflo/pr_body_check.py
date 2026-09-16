#!/usr/bin/env python3
"""Compatibility facade for a mechanism moved to the ``loopzero`` package."""

import os as _os
import sys as _sys

_SOURCE_PATH = _os.path.realpath(__file__)
_KEY = (
    "agent_runtimes/" + _os.path.basename(_SOURCE_PATH)
    if _os.path.basename(_os.path.dirname(_SOURCE_PATH)) == "agent_runtimes"
    else _os.path.basename(_SOURCE_PATH)
)
# Shims that may execute from a trusted snapshot while holding closeout, lease,
# sandbox, or credential authority.
#
# Invariant: a privileged shim contains no interpreter or package selection
# logic and never re-executes. It runs under the interpreter that launched it
# (``sys.executable``) and imports ``loopzero`` only from that interpreter's
# own site-packages. It ignores every environment variable, every command-line
# operand and every path on disk (its own location, the working directory,
# Git metadata, launcher records) for that decision. The trust decision belongs
# to the launcher: the closeout controller runs a copied shim with the
# interpreter it validated. A candidate that runs a copied shim with its own
# interpreter gains nothing: guard authority comes from the lease descriptor
# and nonce the candidate does not hold, not from the shim's own path or from
# the interpreter that executes it. A shim run under an interpreter without
# the package, or under an interpreter whose virtualenv, base installation or
# executable lies inside a linked worktree, refuses with a typed error before
# touching anything. It never imports from the directory it sits in.
_PRIVILEGED = frozenset(
    {
        "worktree_guard.py",
        "pr_closeout.py",
        "trusted_executable.py",
        "job_store.py",
        "guardian_sandbox.py",
        "git_config_security.py",
        "patch_identity.py",
        "agent_runtimes/claude_credential.py",
        "agent_runtimes/claude_token.py",
        "agent_runtimes/codex_credential.py",
        "agent_runtimes/codex_isolation.py",
        "agent_runtimes/cursor_credential.py",
        "agent_runtimes/sdk_bridge.py",
    }
)
_LAUNCHER_KEY = "pr_closeout.py"


def _drop_snapshot_import_roots() -> None:
    """A privileged copy run as a program never imports from where it sits.

    An interpreter started without ``-I``/``-P`` puts the script directory
    first on ``sys.path`` and ``PYTHONPATH`` can name it as well; a module
    planted beside a privileged copy (``argparse.py``, ``json.py``) would then
    shadow the standard library for every later import. Only ``os`` and
    ``sys``, already loaded at interpreter startup, are imported before this
    runs. A host that imports a privileged facade in-process owns its own
    ``sys.path``; the facade itself never inserts its directory or parent in
    any mode.
    """
    roots = {
        _os.path.dirname(_SOURCE_PATH),
        _os.path.dirname(_os.path.dirname(_SOURCE_PATH)),
    }
    _sys.path[:] = [
        entry for entry in _sys.path if _os.path.realpath(entry) not in roots
    ]


if _KEY in _PRIVILEGED and __name__ == "__main__":
    _drop_snapshot_import_roots()

import types as _types  # noqa: E402
from dataclasses import replace as _replace  # noqa: E402
from importlib import import_module as _import_module  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_SOURCE = _Path(_SOURCE_PATH)
# Consumer identity that must hold even when no profile can be located, for
# example when a trusted snapshot of this file runs outside a checkout.
_CONSUMER_ENV_PREFIX = "INTELFLO"
_CONSUMER_VENV = _Path("fastapi_backend/.venv")
_CONSUMER_INTERPRETER = _CONSUMER_VENV / "bin" / "python"
_CONSUMER_BRIDGE = _Path("scripts/util/agent_runtimes/sdk_bridge.py")
_CONSUMER_PROFILE = "workflow.toml"
# Explicit developer binding for a copied developer shim; outranks Git discovery.
_EXPLICIT_ROOT_ENV = ("INTELFLO_ROOT", "LOOPZERO_ROOT")


class _BootstrapRefused(RuntimeError):
    """A fail-closed refusal raised before the package or a profile is touched."""


def _checkout_root(root: _Path) -> bool:
    """Whether ``root`` owns Git metadata and the consumer profile in place.

    Symlinks never qualify. A ``.git`` directory is a primary checkout; a
    ``.git`` file is a linked worktree.
    """
    git = root / ".git"
    profile = root / _CONSUMER_PROFILE
    try:
        if git.is_symlink() or profile.is_symlink():
            return False
        return (git.is_dir() or git.is_file()) and profile.is_file()
    except OSError:
        return False


def _inside_linked_worktree(path: _Path) -> bool:
    """Whether ``path`` lies below a linked worktree (an ancestor with a ``.git`` file)."""
    for ancestor in (path, *path.parents):
        git = ancestor / ".git"
        try:
            if git.is_symlink() or git.is_file():
                return True
        except OSError:
            continue
    return False


def _candidate_interpreter() -> bool:
    """Whether the running interpreter's identity lies inside a linked worktree.

    The identity is ``sys.prefix`` (the virtualenv), ``sys.base_prefix`` (its
    base installation) and ``sys.executable``, each as Python spells it and
    as resolved: a symlinked virtualenv inside a candidate worktree resolves
    its executable to an external base interpreter, so the resolved binary
    alone proves nothing. A copied shim has no trusted way to obtain a
    registered worktree list; the launcher refuses these locations against
    the primary's registered worktrees before it starts any interpreter.
    """
    for spelled in (_sys.prefix, _sys.base_prefix, _sys.executable):
        if not spelled:
            continue
        path = _Path(spelled)
        if _inside_linked_worktree(path) or _inside_linked_worktree(
            _Path(_os.path.realpath(path))
        ):
            return True
    return False


def _interpreter_package() -> _Path | None:
    """``loopzero`` as installed for the running interpreter itself, or None.

    Only a package inside the interpreter's own site-packages qualifies: the
    launcher chose that interpreter, whereas ``PYTHONPATH`` or a working
    directory entry could be candidate-controlled. Nothing is imported here.
    """
    import importlib.util
    import sysconfig

    try:
        spec = importlib.util.find_spec("loopzero")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    origin = _Path(spec.origin).resolve()
    for key in ("purelib", "platlib"):
        try:
            site = _Path(sysconfig.get_paths()[key]).resolve()
        except (KeyError, OSError):
            continue
        if origin.is_relative_to(site):
            return origin.parent
    return None


def _interpreter_checkout() -> _Path | None:
    """The consumer checkout whose virtualenv is the running interpreter.

    Derived from ``sys.prefix`` alone, a property of the interpreter the
    launcher chose; never from this file's location or the environment. It
    supplies the profile only: the package always comes from the interpreter.
    """
    prefix = _Path(_sys.prefix).resolve()
    depth = len(_CONSUMER_VENV.parts)
    if tuple(prefix.parts[-depth:]) != _CONSUMER_VENV.parts or len(prefix.parts) <= depth:
        return None
    root = prefix.parents[depth - 1]
    return root if _checkout_root(root) else None


def _source_root() -> _Path | None:
    """Developer shims only: the checkout this file sits in, by exact layout.

    This file must sit at its historical relative path (``scripts/util``,
    ``scripts/util/agent_runtimes`` or ``scripts/hooks``) directly below a
    checkout that owns its Git metadata and profile. Ancestors are never
    scanned. Privileged shims never consult this.
    """
    parent = _SOURCE.parent
    if parent.name == "agent_runtimes":
        expected = ("scripts", "util", "agent_runtimes")
    elif parent.name == "hooks":
        expected = ("scripts", "hooks")
    else:
        expected = ("scripts", "util")
    depth = len(expected)
    if len(_SOURCE.parts) <= depth + 1:
        return None
    if tuple(_SOURCE.parts[-depth - 1 : -1]) != expected:
        return None
    root = _SOURCE.parents[depth]
    return root if _checkout_root(root) else None


def _explicit_root(value) -> _Path | None:
    """Developer shims only: an absolute checkout named by the developer."""
    if not value:
        return None
    root = _Path(value)
    if not root.is_absolute():
        return None
    try:
        root = root.resolve(strict=True)
    except OSError:
        return None
    return root if _checkout_root(root) else None


def _git_output(*args: str) -> _Path | None:
    import subprocess

    try:
        completed = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_CONFIG_GLOBAL": _os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout.strip()
    if completed.returncode != 0 or not output:
        return None
    path = _Path(output)
    return path if path.is_absolute() else _Path.cwd() / path


def _git_root() -> _Path | None:
    """Developer-only discovery from the working directory's Git metadata."""
    candidates = []
    toplevel = _git_output("rev-parse", "--show-toplevel")
    if toplevel is not None:
        candidates.append(toplevel)
    common = _git_output("rev-parse", "--git-common-dir")
    if common is not None:
        candidates.append(common.resolve().parent)
    for candidate in candidates:
        try:
            if (candidate / _CONSUMER_PROFILE).is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _registered_worktrees(primary: _Path) -> list[_Path]:
    """Every worktree registered with ``primary``, resolved; fails closed."""
    import subprocess

    try:
        completed = subprocess.run(
            ["/usr/bin/git", "-C", str(primary), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_CONFIG_GLOBAL": _os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _BootstrapRefused(
            f"cannot enumerate the primary's registered worktrees: {exc}"
        ) from exc
    if completed.returncode != 0:
        raise _BootstrapRefused(
            "cannot enumerate the primary's registered worktrees: "
            f"{completed.stderr.strip()}"
        )
    prefix = "worktree "
    return [
        _Path(_os.path.realpath(line[len(prefix) :]))
        for line in completed.stdout.splitlines()
        if line.startswith(prefix)
    ]


def _below(path: _Path, root: _Path) -> bool:
    return path == root or path.is_relative_to(root)


def _virtualenv_home(config: _Path) -> _Path:
    """The base installation ``pyvenv.cfg`` names (``sys.base_prefix``'s bin)."""
    for line in config.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "home":
            home = _Path(value.strip())
            if home.is_absolute():
                return _Path(_os.path.realpath(home))
    raise _BootstrapRefused("the primary backend virtualenv names no base installation")


def _launcher_bootstrap() -> tuple[_Path, _Path]:
    """Bind the closeout launcher (``pr_closeout.py`` run as a program).

    The operator names the canonical primary by the absolute path they invoke
    (``/usr/bin/python3 -I <primary>/scripts/util/pr_closeout.py``); the
    repository policy treats that path as a trust input and the package
    controller re-validates it as the canonical primary before anything
    privileged happens. The launcher is the one place that selects the
    interpreter for the copied shims it runs: the primary's backend
    virtualenv, whose identity (the virtualenv directory, the base
    installation its ``pyvenv.cfg`` names, and the real executable) is
    validated here before any interpreter starts. The virtualenv must lie
    inside the primary and outside every worktree registered with it; the
    base installation and the real executable, a regular account- or
    root-owned file, must lie outside the primary and every registered
    worktree, as the kernel job runner requires. The registered list, not an
    ancestor ``.git`` heuristic, decides: a symlinked virtualenv whose
    executable resolves outside a candidate worktree, or a registered
    worktree whose ``.git`` file was removed, is refused all the same. When
    the launching interpreter does not carry the package, the launcher serves
    it from that validated virtualenv's site-packages for the running Python
    version only. It never re-executes.
    """
    expected = ("scripts", "util")
    if (
        len(_SOURCE.parts) <= len(expected) + 1
        or tuple(_SOURCE.parts[-len(expected) - 1 : -1]) != expected
    ):
        raise _BootstrapRefused(
            "the closeout launcher must be invoked at <primary>/scripts/util/pr_closeout.py"
        )
    primary = _SOURCE.parents[len(expected)]
    git = primary / ".git"
    profile = primary / _CONSUMER_PROFILE
    try:
        if git.is_symlink() or not git.is_dir() or profile.is_symlink() or not profile.is_file():
            raise _BootstrapRefused(
                "the closeout launcher must be invoked from the canonical primary "
                "checkout (a .git directory and a regular workflow.toml)"
            )
        venv = _Path(_os.path.realpath(primary / _CONSUMER_VENV))
        config = venv / "pyvenv.cfg"
        if config.is_symlink() or not config.is_file():
            raise _BootstrapRefused(
                "the primary backend virtualenv has no regular pyvenv.cfg"
            )
        home = _virtualenv_home(config)
        interpreter = primary / _CONSUMER_INTERPRETER
        real = _Path(_os.path.realpath(interpreter))
        status = real.stat()
    except OSError as exc:
        raise _BootstrapRefused(
            f"the primary backend interpreter is unavailable: {exc}"
        ) from exc
    if not real.is_file() or not _os.access(real, _os.X_OK):
        raise _BootstrapRefused("the primary backend interpreter is not an executable file")
    if status.st_uid not in {0, _os.getuid()}:
        raise _BootstrapRefused("the primary backend interpreter is not owned by this account")
    primary_real = _Path(_os.path.realpath(primary))
    linked = [
        worktree
        for worktree in _registered_worktrees(primary)
        if worktree != primary_real
    ]
    identity = (
        ("virtualenv", venv),
        ("base installation", home),
        ("interpreter", real),
    )
    for label, path in identity:
        for worktree in linked:
            if _below(path, worktree):
                raise _BootstrapRefused(
                    f"the primary backend {label} ({path}) lies inside the "
                    f"registered worktree {worktree}"
                )
        if _inside_linked_worktree(path):
            raise _BootstrapRefused(
                f"the primary backend {label} ({path}) lies inside a linked worktree"
            )
    if not _below(venv, primary_real):
        raise _BootstrapRefused(
            f"the primary backend virtualenv ({venv}) must lie inside the primary"
        )
    for label, path in identity[1:]:
        if _below(path, primary_real):
            raise _BootstrapRefused(
                f"the primary backend {label} ({path}) must lie outside the "
                "repository, as the kernel job runner requires"
            )
    if _interpreter_package() is None and "loopzero.kernel.settings" not in _sys.modules:
        version = f"python{_sys.version_info[0]}.{_sys.version_info[1]}"
        site = primary / _CONSUMER_VENV / "lib" / version / "site-packages"
        if not (site / "loopzero" / "__init__.py").is_file():
            raise _BootstrapRefused(
                f"the primary backend virtualenv provides no loopzero package for {version}"
            )
        _sys.path.insert(0, str(site))
    return primary, interpreter


def _resolve_root() -> tuple[_Path | None, str]:
    """Bind the package and the consumer profile in explicit trust order.

    Privileged shims (``_PRIVILEGED``):
    1. ``launcher`` (``pr_closeout.py`` run as a program only): the operator's
       primary, see ``_launcher_bootstrap``.
    2. ``interpreter``: ``loopzero`` installed for ``sys.executable``. The
       profile, if any, comes from the checkout whose virtualenv that
       interpreter is (``sys.prefix``). An interpreter whose virtualenv,
       base installation or executable lies inside a linked worktree is
       refused (``candidate-interpreter``).
    3. ``in-process``: a copy executed inside a process that already imported
       the package (the closeout controller loads the snapshot guard with
       ``runpy``) binds to that process's package and configuration.
    Anything else is ``unbound`` and refused: no environment variable,
    operand, working directory or checkout on disk is ever consulted.

    Developer shims keep a lazy checkout resolution for convenience, for the
    profile and, only when the interpreter carries no package, for the
    checkout virtualenv's site-packages of the running Python version:
    ``source`` (exact layout), ``explicit-env`` (``INTELFLO_ROOT`` /
    ``LOOPZERO_ROOT``), ``interpreter``, then ``git`` discovery. No shim of
    either kind ever re-executes an interpreter.
    """
    if _KEY == _LAUNCHER_KEY and __name__ == "__main__":
        primary, _ = _launcher_bootstrap()
        return primary, "launcher"
    if _KEY in _PRIVILEGED:
        if _candidate_interpreter():
            return None, "candidate-interpreter"
        if _interpreter_package() is not None:
            return _interpreter_checkout(), "interpreter"
        if "loopzero.kernel.settings" in _sys.modules:
            return None, "in-process"
        return None, "unbound"
    source = _source_root()
    if source is not None:
        return source, "source"
    for name in _EXPLICIT_ROOT_ENV:
        explicit = _explicit_root(_os.environ.get(name))
        if explicit is not None:
            return explicit, "explicit-env"
    if _interpreter_package() is not None:
        return _interpreter_checkout(), "interpreter"
    if "loopzero.kernel.settings" in _sys.modules:
        return None, "in-process"
    discovered = _git_root()
    if discovered is not None:
        return discovered, "git"
    return None, "unbound"


def _refuse(message: str) -> None:
    text = f"{_SOURCE.name}: refusing to run: {message}"
    if __name__ == "__main__":
        print(text, file=_sys.stderr)
        raise SystemExit(2)
    raise ImportError(text)


try:
    _ROOT, _ROOT_BINDING = _resolve_root()
except _BootstrapRefused as _exc:
    _refuse(str(_exc))
_IN_PROCESS = _ROOT_BINDING == "in-process"
if _ROOT_BINDING == "candidate-interpreter":
    _refuse(
        f"the interpreter running this privileged shim ({_sys.executable}, "
        f"virtualenv {_sys.prefix}) lies inside a linked worktree; a candidate "
        "worktree never supplies the interpreter of a privileged shim"
    )
if _ROOT_BINDING == "unbound" and _KEY in _PRIVILEGED:
    _refuse(
        f"the loopzero package is not installed for the interpreter running this "
        f"privileged shim ({_sys.executable}); the launcher must run it with the "
        "validated consumer interpreter, and no environment variable, operand or "
        "checkout on disk can supply one"
    )
if (
    _ROOT is not None
    and _ROOT_BINDING in {"source", "explicit-env", "git"}
    and _interpreter_package() is None
):
    # Developer convenience only: serve the resolved checkout's package to an
    # interpreter that carries none, for the running Python version.
    _SITE_PACKAGES = (
        _ROOT
        / _CONSUMER_VENV
        / "lib"
        / f"python{_sys.version_info[0]}.{_sys.version_info[1]}"
        / "site-packages"
    )
    if _SITE_PACKAGES.is_dir() and str(_SITE_PACKAGES) not in _sys.path:
        _sys.path.insert(0, str(_SITE_PACKAGES))

from loopzero.kernel import settings as _kernel_settings  # noqa: E402
from loopzero.runners import settings as _runtime_settings  # noqa: E402

if _KEY not in _PRIVILEGED:
    # Developer shims keep their historical sibling imports for overlays.
    for _IMPORT_ROOT in (_SOURCE.parent, _SOURCE.parent.parent):
        if str(_IMPORT_ROOT) not in _sys.path:
            _sys.path.insert(0, str(_IMPORT_ROOT))
_TARGETS = {
    "agent_event.py": "loopzero.kernel.events",
    "skill_run_identity.py": "loopzero.kernel.run_identity",
    "skill_run_log.py": "loopzero.kernel.run_log",
    "delivery_liveness.py": "loopzero.kernel.liveness",
    "dispatch_common.py": "loopzero.kernel.gitscope",
    "patch_identity.py": "loopzero.kernel.patch_identity",
    "git_config_security.py": "loopzero.kernel.git_config_security",
    "dispatch_ledger.py": "loopzero.kernel.ledger",
    "dispatch_authority.py": "loopzero.kernel.authority",
    "dispatch_authority_store.py": "loopzero.kernel.authority_store",
    "dispatch_authority_projection.py": "loopzero.kernel.authority_projection",
    "dispatch_ledger_lifecycle.py": "loopzero.kernel.ledger_lifecycle",
    "worktree_guard.py": "loopzero.kernel.worktree_lease",
    "worktree_claims.py": "loopzero.kernel.worktree_claims",
    "worktree_list.py": "loopzero.kernel.worktree_list",
    "worktree_prune.py": "loopzero.kernel.worktree_prune",
    "guardian_sandbox.py": "loopzero.kernel.sandbox",
    "trusted_executable.py": "loopzero.kernel.trusted_exec",
    "job_store.py": "loopzero.kernel.jobs",
    "size_lint.py": "loopzero.hooks.size_lint",
    "dispatch_acceptance.py": "loopzero.review.acceptance",
    "dispatch_acceptance_grammar.py": "loopzero.review._acceptance_grammar",
    "dispatch_routing.py": "loopzero.review.routing",
    "review_gate_preflight.py": "loopzero.review.preflight",
    "review_chain.py": "loopzero.review.chain",
    "review_tree_coverage.py": "loopzero.review._tree_coverage",
    "dispatch_review_authority.py": "loopzero.review.authority",
    "dispatch_archived_review.py": "loopzero.review._archived",
    "dispatch_review_evidence.py": "loopzero.review.evidence",
    "delivery_review_risk.py": "loopzero.review.risk",
    "security_review_scope.py": "loopzero.review._security_scope",
    "ci_path_classifier.py": "loopzero.review._ci_path_classifier",
    "cross_harness_review.py": "loopzero.review.harness",
    "finding_ledger.py": "loopzero.review.findings",
    "pr_body_check.py": "loopzero.delivery._body_check",
    "pr_publish.py": "loopzero.delivery.publish",
    "pr_publish_body.py": "loopzero.delivery._publish_body",
    "pr_publish_equivalence.py": "loopzero.delivery._publish_equivalence",
    "pr_publish_findings.py": "loopzero.delivery._publish_findings",
    "pr_publish_obligations.py": "loopzero.delivery._publish_obligations",
    "pr_publish_paths.py": "loopzero.delivery._publish_paths",
    "pr_publish_remote.py": "loopzero.delivery._publish_remote",
    "pr_publish_risk.py": "loopzero.delivery._publish_risk",
    "pr_publish_threads.py": "loopzero.delivery._publish_threads",
    "pr_closeout.py": "loopzero.delivery.closeout",
    "delivery_review_reentry.py": "loopzero.delivery.reentry",
    "guardian_state.py": "loopzero.guardian.state",
    "guardian_audit_deposit.py": "loopzero.guardian.deposit",
    "guardian_risk.py": "loopzero.guardian.risk",
    "guardian_tick.py": "loopzero.guardian.ratchet",
    "agent_runtimes/__init__.py": "loopzero.runners",
    "agent_runtimes/contracts.py": "loopzero.runners.contract",
    "agent_runtimes/process.py": "loopzero.runners.process",
    "agent_runtimes/claude.py": "loopzero.runners.claude",
    "agent_runtimes/claude_credential.py": "loopzero.runners.claude",
    "agent_runtimes/claude_token.py": "loopzero.runners.claude",
    "agent_runtimes/codex.py": "loopzero.runners.codex",
    "agent_runtimes/codex_credential.py": "loopzero.runners.codex",
    "agent_runtimes/codex_isolation.py": "loopzero.runners.codex",
    "agent_runtimes/cursor.py": "loopzero.runners.cursor",
    "agent_runtimes/cursor_credential.py": "loopzero.runners.cursor",
    "agent_runtimes/registry.py": "loopzero.runners.registry",
    "agent_runtimes/sdk_bridge.py": "loopzero.runners.bridge",
}

_PROFILE = None
_IMPLEMENTATION = None


def _profile():
    """Load and validate the consumer profile once, on first mechanism use."""
    global _PROFILE
    if _PROFILE is None and _ROOT is not None:
        from loopzero.config import load_profile

        _PROFILE = load_profile(_ROOT)
    return _PROFILE


def _real_interpreter(root: _Path) -> _Path | None:
    """The symlink-free interpreter the kernel sandbox may bind; None if absent."""
    candidate = root / _CONSUMER_INTERPRETER
    try:
        if candidate.is_file():
            return _Path(_os.path.realpath(candidate))
    except OSError:
        return None
    return None


def _kernel_from_profile(profile):
    """Kernel settings whose toolchain interpreter is the resolved real path."""
    toolchain = dict(profile.toolchain)
    real = _real_interpreter(_ROOT)
    if real is not None:
        toolchain["interpreter"] = str(real)
    return _replace(
        _kernel_settings.KernelSettings.from_profile(profile), toolchain=toolchain
    )


def _configure() -> None:
    profile = _profile()
    if profile is None:
        if (
            _IN_PROCESS
            and _kernel_settings.settings.env_prefix == _CONSUMER_ENV_PREFIX
        ):
            # The hosting process already configured this consumer; a copied
            # shim must not replace its validated settings.
            return
        _kernel_settings.configure(
            _kernel_settings.KernelSettings(
                env_prefix=_CONSUMER_ENV_PREFIX,
                toolchain={"interpreter": str(_CONSUMER_INTERPRETER)},
            )
        )
        _runtime_settings._SETTINGS.set(
            _runtime_settings.RuntimeSettings(
                env_prefix=_CONSUMER_ENV_PREFIX,
                toolchain_interpreter=_CONSUMER_INTERPRETER,
                bridge_path=_CONSUMER_BRIDGE,
            )
        )
        return
    kernel = _kernel_from_profile(profile)
    # Kernel mechanisms bind settings at import, so configure before the review
    # package (which imports them) and pin the sandbox-relevant modules now.
    _kernel_settings.configure(kernel)
    for _name in ("sandbox", "validation", "patch_identity", "jobs"):
        _import_module(f"loopzero.kernel.{_name}")
    _runtime_settings._SETTINGS.set(
        _replace(
            _runtime_settings.RuntimeSettings.from_profile(profile),
            bridge_path=_ROOT / _CONSUMER_BRIDGE,
        )
    )
    from loopzero import review as _review
    from loopzero.review import _security_scope

    _review.configure(profile)
    # review.configure re-derives kernel settings from the profile alone;
    # restore the resolved interpreter for sandbox mount validation.
    _kernel_settings.configure(kernel)
    # The configured section list names the full chain; the classifier only
    # adds the security section when a security pattern matches (D23).
    _security_scope.configure(
        security_patterns=tuple(profile.security_patterns),
        required_sections=tuple(
            section for section in profile.required_sections if section != "security"
        ),
    )


def _activate():
    """Configure the consumer once and bind the package implementation."""
    global _IMPLEMENTATION
    if _IMPLEMENTATION is None:
        _configure()
        implementation = _import_module(_TARGETS[_KEY])
        namespace = globals()
        for name in dir(implementation):
            if not name.startswith("__") and name not in _RESERVED:
                namespace[name] = getattr(implementation, name)
        namespace["__doc__"] = implementation.__doc__
        _IMPLEMENTATION = implementation
    return _IMPLEMENTATION


def __getattr__(name: str):
    if name.startswith("__"):
        raise AttributeError(name)
    implementation = _activate()
    try:
        return getattr(implementation, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__():
    _activate()
    return sorted(globals())


class _LoopZeroFacadeModule(_types.ModuleType):
    """Keep public assignments visible on both the facade and its target.

    Activation precedes every public assignment so a value assigned before the
    first read is not replaced by the implementation copy, and the assigned
    object is what both namespaces expose afterwards.
    """

    def __setattr__(self, name: str, value: object) -> None:
        if name.startswith("__") or name in _RESERVED:
            super().__setattr__(name, value)
            return
        implementation = _activate()
        super().__setattr__(name, value)
        setattr(implementation, name, value)

    def __delattr__(self, name: str) -> None:
        if name.startswith("__") or name in _RESERVED:
            super().__delattr__(name)
            return
        implementation = _activate()
        super().__delattr__(name)
        if hasattr(implementation, name):
            delattr(implementation, name)


_MODULE = _sys.modules.get(__name__)
if _MODULE is not None and _MODULE.__dict__ is globals():
    _MODULE.__class__ = _LoopZeroFacadeModule


def _call_entry(entry, argv):
    import inspect

    try:
        parameters = inspect.signature(entry).parameters
    except (TypeError, ValueError):
        parameters = {}
    return entry(argv) if parameters else entry()


_HELD_INTERPRETERS = []


def _pinned_interpreter(interpreter: _Path):
    """Pin ``interpreter`` through the primary's own ``loopzero_interpreter.py``.

    The launcher runs at ``<primary>/scripts/util/pr_closeout.py`` and the
    primary is the operator's trust input, so the sibling module is loaded
    from that validated location by explicit path: ``sys.path`` is never
    extended, and a copied launcher never reaches this point.
    """
    import importlib.util

    module_path = _ROOT / "scripts" / "util" / "loopzero_interpreter.py"
    status = module_path.lstat()
    if module_path.is_symlink() or not module_path.is_file():
        raise _BootstrapRefused(f"{module_path} is not a regular file")
    if status.st_uid not in {0, _os.getuid()}:
        raise _BootstrapRefused(f"{module_path} is not owned by this account")
    spec = importlib.util.spec_from_file_location("_intelflo_interpreter", module_path)
    if spec is None or spec.loader is None:
        raise _BootstrapRefused(f"{module_path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.pinned_interpreter(interpreter)


def _run_main(argv):
    """Run the historical command line of this path with its real entry point."""
    _activate()
    namespace = globals()
    if _KEY == _LAUNCHER_KEY:
        # Hand the controller the interpreter this launcher validated without
        # a mutable pathname: the launch file is pinned by a descriptor this
        # process holds for its lifetime and consumed as
        # /proc/<pid>/fd/<n>, so no consumer reopens a same-account pathname
        # later. Every copied shim the controller runs then executes under
        # the validated real executable as the primary's virtualenv
        # interpreter and binds the package installed for it.
        try:
            pinned = _pinned_interpreter(_ROOT / _CONSUMER_INTERPRETER)
        except (OSError, ValueError, _BootstrapRefused) as exc:
            print(f"{_SOURCE.name}: cannot bind the validated interpreter: {exc}", file=_sys.stderr)
            return 2
        _HELD_INTERPRETERS.append(pinned)
        _IMPLEMENTATION.TRUSTED_PYTHON = pinned.reference
        return namespace["main"](argv, profile=_profile(), launcher_path=_SOURCE)
    if _KEY == "agent_runtimes/sdk_bridge.py":
        import asyncio

        bootstrap = (
            _runtime_settings.RuntimeSettings.from_environment()
            if _runtime_settings.SETTINGS_ENV in _os.environ
            else _runtime_settings.get_settings()
        )
        with bootstrap.use():
            if argv == ["--codex-refresh"]:
                return namespace["codex_refresh"]()
            return asyncio.run(namespace["main"]())
    if _KEY == "agent_event.py":
        try:
            return _call_entry(namespace["main"], argv)
        except Exception as exc:  # noqa: BLE001 - historical hook contract
            print(f"agent_event: warning — {exc}", file=_sys.stderr)
            return 0
    entry = namespace.get("main")
    if not callable(entry):
        entry = namespace.get("_main")
    if not callable(entry):
        print(f"{_SOURCE.name}: this module has no command-line entry point", file=_sys.stderr)
        return 2
    return _call_entry(entry, argv)


_RESERVED = frozenset(globals())

# A caller that executes this file with ``runpy.run_path`` (the closeout
# controller loads the snapshot guard that way) receives a plain namespace
# copy, which module ``__getattr__`` cannot serve; bind the package eagerly.
if __name__ != "__main__" and globals().get("__spec__") is None:
    _activate()

if __name__ == "__main__":
    raise SystemExit(_run_main(_sys.argv[1:]))
