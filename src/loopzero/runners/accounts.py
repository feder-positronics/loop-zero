"""Single declared native account selection owned by the trusted host caller.

Declarations never cross the child boundary. A context owns a broker snapshot;
children receive independent read descriptions and a narrowing vendor latch.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from .settings import RuntimeSettings, get_settings


class DeclaredAccountError(RuntimeError):
    """A declared account cannot be used safely (content-free diagnostic)."""


@dataclass(frozen=True, slots=True)
class Account:
    kind: str
    credential_path: Path = field(repr=False)


def parse_accounts(value: object) -> Mapping[str, Account] | None:
    """Copy a closed, single-account-per-vendor declaration; None is legacy."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DeclaredAccountError("accounts must be a table")
    result = {}
    for vendor, entries in value.items():
        if (
            vendor not in {"claude", "codex", "cursor"}
            or not isinstance(entries, Mapping)
            or len(entries) != 1
        ):
            raise DeclaredAccountError(
                "accounts require one supported account per vendor"
            )
        label, entry = next(iter(entries.items()))
        if (
            not isinstance(label, str)
            or not label
            or not isinstance(entry, Mapping)
            or set(entry) != {"kind", "credential_path"}
        ):
            raise DeclaredAccountError("account declaration fields are invalid")
        kind, path = entry["kind"], entry["credential_path"]
        if not isinstance(kind, str) or kind not in (
            {"oauth-login", "setup-token"} if vendor == "claude" else {"oauth-login"}
        ):
            raise DeclaredAccountError("account credential kind is unsupported")
        if (
            not isinstance(path, str)
            or not path
            or not Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise DeclaredAccountError("account credential path must be absolute")
        result[vendor] = Account(kind, Path(path))
    return MappingProxyType(result)


def freeze_accounts(value: object) -> Mapping[str, Account] | None:
    if value is None:
        return None
    if isinstance(value, Mapping) and all(
        isinstance(v, Account) for v in value.values()
    ):
        if any(k not in {"claude", "codex", "cursor"} for k in value):
            raise DeclaredAccountError("account vendor is unsupported")
        # Revalidate even public Account instances supplied by host callers.
        return parse_accounts(
            {
                k: {
                    "account": {
                        "kind": v.kind,
                        "credential_path": str(v.credential_path),
                    }
                }
                for k, v in value.items()
            }
        )
    return parse_accounts(value)


@dataclass(frozen=True)
class _Scope:
    owner: RuntimeSettings
    vendor: str
    descriptor: int
    settings: RuntimeSettings


_SCOPE: ContextVar[_Scope | None] = ContextVar("declared_account_scope", default=None)


def credential_descriptor(vendor: str) -> int | None:
    active = _SCOPE.get()
    if active is not None:
        if active.vendor != vendor:
            raise DeclaredAccountError("declared credential vendor mismatch")
        return active.descriptor
    raw = os.environ.get(get_settings().env_name(f"{vendor.upper()}_AUTH_FD"))
    return int(raw) if raw is not None else None


def credential_reference(vendor: str) -> str | None:
    active = _SCOPE.get()
    if active is not None:
        return str(credential_descriptor(vendor))
    return os.environ.get(get_settings().env_name(f"{vendor.upper()}_AUTH_FD"))


def active_scope() -> _Scope | None:
    return _SCOPE.get()


def _validate_source(account, settings, request):
    path = account.credential_path
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise DeclaredAccountError("declared credential source is unsafe")
    metadata = path.stat()
    parent = path.parent.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 2 < metadata.st_size <= 1024 * 1024
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise DeclaredAccountError("declared credential source is unsafe")
    from .process import _private_state_root

    roots = [request.cwd.resolve(), _private_state_root()]
    profile = request.capability_profile
    roots.extend(
        path.resolve()
        for path in (
            *profile.read_roots,
            *profile.evidence_read_roots,
            *profile.write_roots,
        )
    )
    if request.tooling_root is not None:
        roots.append(request.tooling_root.resolve())
    if settings.tooling_root is not None:
        roots.append(settings.tooling_root.resolve())
    if any(
        entry.credential_path.resolve().is_relative_to(root)
        for entry in settings.accounts.values()
        for root in roots
    ) or any((p / ".git").exists() for p in path.parents):
        raise DeclaredAccountError(
            "declared credential source is exposed to the worker"
        )


def _refresh_containment(spec):
    # Reuse the package host broker boundary, constructing it only if renewal
    # is needed. Failure to contain renewal never authorizes ambient fallback.
    from ..credential_seal import _host_refresh_wrapper

    return _host_refresh_wrapper()(spec)


@contextmanager
def credential_scope(settings, vendor, request, *, method=None):
    active = _SCOPE.get()
    if active is not None and (active.owner is not settings or active.vendor != vendor):
        raise DeclaredAccountError("nested declared credential ownership mismatch")
    if settings.accounts is None:
        if settings.declared_credential_vendor is not None:
            raise DeclaredAccountError(
                "child credential context cannot open host adapters"
            )
        yield
        return
    if vendor not in settings.accounts or request.vendor != vendor:
        raise DeclaredAccountError("native vendor has no declared account")
    from . import claude, codex, cursor

    supported = {
        "claude": {*claude.CLAUDE_AUTO_TRANSPORTS, claude.CLAUDE_SDK_TRANSPORT},
        "codex": {*codex.CODEX_AUTO_TRANSPORTS, codex.CODEX_SDK_TRANSPORT},
        "cursor": {"cursor/cli-stream-json"},
    }
    if request.transport not in supported[vendor] or method == "probe_cli":
        raise DeclaredAccountError("declared account requires protected SDK transport")
    if active is not None:
        with active.settings.use():
            yield
        return
    from ..credential_seal import _read_snapshot, _validate_access_only
    from .process import private_temporary_directory

    account = settings.accounts[vendor]
    with ExitStack() as stack:
        try:
            _validate_source(account, settings, request)
            if vendor == "claude":
                actual = claude.explicit_credential_source_kind(account.credential_path)
                if (
                    actual
                    != {"oauth-login": "oauth-file", "setup-token": "setup-token-file"}[
                        account.kind
                    ]
                ):
                    raise DeclaredAccountError("declared credential kind mismatch")
            broker = {
                "claude": claude.claude_subscription_credential,
                "codex": codex.codex_subscription_credential,
                "cursor": cursor.cursor_subscription_credential,
            }[vendor]
            options = {
                "requested_runtime_s": request.timeout_s,
                "credential_path": account.credential_path,
            }
            if vendor in {"claude", "codex"}:
                options["sandbox_wrapper"] = _refresh_containment
            if vendor == "claude":
                options.update(
                    allow_token_fallback=False, raw_token_source="setup-token-file"
                )
            fd = stack.enter_context(broker(**options))
            payload = _read_snapshot(fd)
            _validate_access_only(vendor, payload)
            if vendor == "claude" and (
                "claudeCodeOauthToken" in json.loads(payload)
            ) != (account.kind == "setup-token"):
                raise DeclaredAccountError("declared credential kind mismatch")
            home = stack.enter_context(private_temporary_directory("declared-account"))
            scoped = replace(
                settings, session_home=home, declared_credential_vendor=vendor
            )
        except (OSError, RuntimeError, ValueError, TypeError):
            raise DeclaredAccountError(
                "declared credential is unavailable or unsafe"
            ) from None
        token = _SCOPE.set(_Scope(settings, vendor, fd, scoped))
        try:
            with scoped.use():
                yield
        finally:
            _SCOPE.reset(token)


def declared_launch_command(command, *, cwd):
    """Narrow the trusted bridge entry; never infer permission from child env."""
    scope = _SCOPE.get()
    if scope is None:
        return command
    settings = get_settings()
    bridge = (
        len(command) == 3
        and command[1] == "-I"
        and Path(command[2]).resolve() == settings.bridge(settings.tooling_root or cwd)
    )
    if bridge:
        return [*command, "--require-brokered-credential"]
    # Vendor version commands need neither credentials nor bridge privileges.
    if len(command) == 2 and command[1] == "--version":
        return command
    if (
        len(command) == 4
        and command[1:3] == ["-I", "-c"]
        and command[3] in {"import claude_agent_sdk", "import openai_codex"}
    ):
        return command
    if scope.vendor != "cursor":
        raise DeclaredAccountError("declared account requires protected SDK launch")
    return command


@contextmanager
def declared_launch_environment(environment, pass_fds, *, bridge, sandboxed):
    scope = _SCOPE.get()
    if scope is None:
        yield environment, pass_fds
        return
    if not sandboxed:
        raise DeclaredAccountError("declared account requires contained launch")
    settings = get_settings()
    # Only operational names cross this boundary; custom allowlists cannot
    # restore vendor tokens, auth paths or endpoint/configuration overrides.
    allowed = {
        "PATH",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TERM",
        "NO_COLOR",
        "COLORTERM",
        "FORCE_COLOR",
        "TMPDIR",
        "AGENT_DISPATCH_DEPTH",
    }
    progress = settings.env_name("RUNTIME_PROGRESS_FD")
    allowed.add(progress)
    narrowed = {k: v for k, v in environment.items() if k in allowed}
    narrowed["HOME"] = str(settings.session_home)
    narrowed["XDG_CONFIG_HOME"] = str(settings.session_home / ".config")
    narrowed["XDG_CACHE_HOME"] = str(settings.session_home / ".cache")
    # Preserve only the launch-owned progress channel, never caller auth FDs.
    fds = tuple(fd for fd in pass_fds if str(fd) == environment.get(progress))
    descriptor = None
    try:
        if bridge:
            # dup() shares its read offset. Reopening the held sealed inode
            # gives each readiness/run child its own offset and lifetime.
            descriptor = os.open(
                f"/proc/self/fd/{scope.descriptor}", os.O_RDONLY | os.O_CLOEXEC
            )
            narrowed[settings.env_name(f"{scope.vendor.upper()}_AUTH_FD")] = str(
                descriptor
            )
            narrowed.update(settings.child_environment())
            fds = (*fds, descriptor)
        yield narrowed, fds
    finally:
        if descriptor is not None:
            os.close(descriptor)


def validate_child_credential(vendor: str) -> None:
    """Narrowing gate, not an assertion of account identity or billing."""
    import fcntl

    settings = get_settings()
    if settings.declared_credential_vendor != vendor or vendor not in {
        "claude",
        "codex",
    }:
        raise DeclaredAccountError("declared child credential context is missing")
    try:
        fd = int(os.environ[settings.env_name(f"{vendor.upper()}_AUTH_FD")])
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not 2 < metadata.st_size <= 1024 * 1024
        ):
            raise ValueError
        try:
            required = (
                fcntl.F_SEAL_WRITE
                | fcntl.F_SEAL_GROW
                | fcntl.F_SEAL_SHRINK
                | fcntl.F_SEAL_SEAL
            )
            protected = fcntl.fcntl(fd, fcntl.F_GET_SEALS) & required == required
        except (AttributeError, OSError):
            # Existing brokers use an unlinked, read-only 0400 snapshot when
            # the interpreter does not provide memfd_create.
            protected = (
                metadata.st_nlink == 0
                and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o400
                and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            )
        if not protected:
            raise ValueError
        payload = os.pread(fd, metadata.st_size, 0)
        if vendor == "claude":
            from .claude import protected_claude_credential_ready

            valid = protected_claude_credential_ready(fd)
        else:
            from .codex import protected_codex_credential_ready

            decoded = json.loads(payload)
            valid = (
                protected_codex_credential_ready(payload)
                and set(decoded)
                == {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}
                and set(decoded["tokens"])
                == {"access_token", "refresh_token", "id_token", "account_id"}
                and decoded["tokens"]["refresh_token"]
                == decoded["tokens"]["access_token"]
            )
        if not valid:
            raise ValueError
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        raise DeclaredAccountError("declared child credential is invalid") from None
