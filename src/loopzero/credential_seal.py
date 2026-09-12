"""Trusted host command for producing one access-only runtime credential.

This module belongs to the release wheel, not to the checked-out conformance
code.  It validates and, when necessary, refreshes the selected native-login
credential before writing a bounded access-only snapshot.  Credential bytes
are never written to stdout or stderr.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Callable, ContextManager, Sequence

from .runners import claude, codex, cursor
from .runners.process import LaunchSpec, SandboxWrapper
from .runners.settings import RuntimeSettings

MAX_SNAPSHOT_BYTES = 1024 * 1024
# Covers the complete seven-scenario job plus broker safety margin.  The
# in-sandbox broker rechecks the shorter per-launch horizon without refreshing.
SEALED_RUNTIME_S = 15 * 60
VENDORS = ("claude", "codex", "cursor")


class CredentialSealError(RuntimeError):
    """The host could not safely create an access-only snapshot."""


def _inside_repository(path: Path) -> bool:
    candidate = path if path.is_dir() else path.parent
    for parent in (candidate, *candidate.parents):
        marker = parent / ".git"
        if marker.is_file() or (marker / "HEAD").is_file():
            return True
    return False


def _validate_paths(source: Path | None, output: Path) -> tuple[Path | None, Path]:
    if not output.is_absolute() or (source is not None and source == output):
        raise CredentialSealError("credential paths are invalid")
    if source is not None and not source.is_absolute():
        raise CredentialSealError("credential paths are invalid")
    source_alias = source.is_symlink() if source is not None else False
    output_parent_alias = output.parent.is_symlink()
    try:
        output_parent = output.parent.resolve(strict=True)
        parent_metadata = output_parent.stat(follow_symlinks=False)
        if source is not None:
            source = source.resolve(strict=True)
            source_metadata = source.stat(follow_symlinks=False)
        else:
            source_metadata = None
    except OSError as exc:
        raise CredentialSealError("credential path is unavailable") from exc
    output = output_parent / output.name
    workspace_value = os.environ.get("GITHUB_WORKSPACE")
    workspace = (
        Path(workspace_value).resolve(strict=False)
        if workspace_value
        else None
    )
    if (
        source_alias
        or (
            source_metadata is not None
            and (
                not stat.S_ISREG(source_metadata.st_mode)
                or source_metadata.st_uid != os.getuid()
                or source_metadata.st_nlink != 1
                or stat.S_IMODE(source_metadata.st_mode) != 0o600
                or source_metadata.st_size <= 2
                or source_metadata.st_size > MAX_SNAPSHOT_BYTES
            )
        )
        or output.exists()
        or output_parent_alias
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or (source is not None and _inside_repository(source))
        or _inside_repository(output)
        or (
            workspace is not None
            and (
                (source is not None and source.is_relative_to(workspace))
                or output.is_relative_to(workspace)
            )
        )
    ):
        raise CredentialSealError("credential path is unsafe")
    return source, output


def _host_refresh_wrapper() -> SandboxWrapper:
    """Build the release-owned positive filesystem allowlist for refresh only."""

    bwrap_value = shutil.which("bwrap")
    if bwrap_value is None:
        raise CredentialSealError("credential refresh containment is unavailable")
    bwrap = str(Path(bwrap_value).resolve(strict=True))
    runtime_root_value = os.environ.get("LOOPZERO_LIVE_RUNTIME_ROOT")
    readonly_roots = [Path("/usr"), Path(sys.prefix).resolve()]
    if runtime_root_value:
        readonly_roots.append(Path(runtime_root_value).resolve(strict=True))

    def wrapper(spec: LaunchSpec) -> Sequence[str]:
        command = [
            bwrap,
            "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--tmpfs", "/tmp", "--tmpfs", "/run", "--dev", "/dev",
            "--proc", "/proc",
        ]
        for root in (Path("/bin"), Path("/lib"), Path("/lib64"), Path("/sbin")):
            if root.is_symlink():
                command.extend(("--symlink", os.readlink(root), str(root)))
            elif root.is_dir():
                command.extend(("--ro-bind", str(root), str(root)))
        seen: set[Path] = set()
        for root in readonly_roots:
            if root in seen or root.is_relative_to(Path("/usr")) and root != Path("/usr"):
                continue
            seen.add(root)
            for parent in sorted(root.parents, key=lambda item: len(item.parts)):
                if parent != Path("/"):
                    command.extend(("--dir", str(parent)))
            command.extend(("--ro-bind", str(root), str(root)))
        for system_file in (
            Path("/etc/alternatives"), Path("/etc/ssl"), Path("/etc/ld.so.cache"),
            Path("/etc/passwd"), Path("/etc/group"), Path("/etc/hosts"),
            Path("/etc/resolv.conf"),
        ):
            if system_file.exists():
                for parent in sorted(
                    system_file.parents, key=lambda item: len(item.parts)
                ):
                    if parent != Path("/"):
                        command.extend(("--dir", str(parent)))
                command.extend(("--ro-bind", str(system_file), str(system_file)))
        mounts = (*spec.private_mounts, spec.private_tmpdir)
        for mount in mounts:
            for parent in sorted(mount.parents, key=lambda item: len(item.parts)):
                if parent != Path("/"):
                    command.extend(("--dir", str(parent)))
            command.extend(("--bind", str(mount), str(mount)))
        command.extend(("--chdir", str(spec.cwd), "--", *spec.argv))
        return command

    return wrapper


def _read_snapshot(descriptor: int) -> bytes:
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 2
            or metadata.st_size > MAX_SNAPSHOT_BYTES
        ):
            raise CredentialSealError("broker returned an invalid snapshot")
        payload = os.pread(descriptor, metadata.st_size, 0)
    except OSError as exc:
        raise CredentialSealError("broker returned an invalid snapshot") from exc
    if len(payload) != metadata.st_size:
        raise CredentialSealError("broker returned an incomplete snapshot")
    return payload


def _validate_access_only(vendor: str, payload: bytes) -> None:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialSealError("broker returned an invalid snapshot") from exc
    valid = False
    if vendor == "claude" and isinstance(decoded, dict):
        oauth = decoded.get("claudeAiOauth")
        valid = (
            set(decoded) == {"claudeAiOauth"}
            and isinstance(oauth, dict)
            and set(oauth) == {
                "accessToken", "refreshToken", "expiresAt",
                "refreshTokenExpiresAt", "scopes", "subscriptionType",
            }
            and isinstance(oauth.get("accessToken"), str)
            and bool(oauth.get("accessToken"))
            and oauth.get("refreshToken") == oauth.get("accessToken")
            and type(oauth.get("expiresAt")) is int
            and oauth.get("refreshTokenExpiresAt") == oauth.get("expiresAt")
        )
        if not valid:
            token = decoded.get("claudeCodeOauthToken")
            valid = (
                set(decoded) == {"claudeCodeOauthToken", "source"}
                and isinstance(token, str)
                and claude.TOKEN_PATTERN.fullmatch(token) is not None
                and decoded.get("source")
                in {"token-env", "token-file", "token-file(default)"}
            )
    elif vendor == "codex" and isinstance(decoded, dict):
        tokens = decoded.get("tokens")
        valid = (
            decoded.get("auth_mode") == "chatgpt"
            and decoded.get("OPENAI_API_KEY") in (None, "")
            and set(decoded) == {
                "auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"
            }
            and isinstance(decoded.get("last_refresh"), str)
            and bool(decoded.get("last_refresh"))
            and isinstance(tokens, dict)
            and set(tokens) == {
                "access_token", "id_token", "refresh_token", "account_id"
            }
            and isinstance(tokens.get("access_token"), str)
            and bool(tokens.get("access_token"))
            and tokens.get("refresh_token") == tokens.get("access_token")
        )
    elif vendor == "cursor" and isinstance(decoded, dict):
        valid = (
            set(decoded) == {"accessToken", "refreshToken"}
            and isinstance(decoded.get("accessToken"), str)
            and bool(decoded.get("accessToken"))
            and decoded.get("refreshToken") == decoded.get("accessToken")
        )
    if not valid:
        raise CredentialSealError("broker returned a refresh-capable snapshot")


def _write_snapshot(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CredentialSealError("snapshot output is unavailable") from exc
    try:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)


def _broker_for(vendor: str) -> Callable[..., ContextManager[int]]:
    return {
        "claude": claude.claude_subscription_credential,
        "codex": codex.codex_subscription_credential,
        "cursor": cursor.cursor_subscription_credential,
    }[vendor]


def seal_credential(
    vendor: str,
    *,
    source: Path | None = None,
    output: Path,
    broker: Callable[..., ContextManager[int]] | None = None,
    sandbox_wrapper: SandboxWrapper | None = None,
) -> str:
    """Validate/refresh *source* and atomically create access-only *output*."""

    if vendor not in VENDORS:
        raise CredentialSealError("credential vendor is invalid")
    source, output = _validate_paths(source, output)
    wrapper = sandbox_wrapper or _host_refresh_wrapper()
    runtime_value = os.environ.get("LOOPZERO_LIVE_CLI_PATH")
    runtime = Path(runtime_value).resolve(strict=True) if runtime_value else None
    state_root = output.parent / ".loopzero-credential-broker-state"
    discovery_state_root = os.environ.get("LOOPZERO_LIVE_STATE_ROOT")
    settings = RuntimeSettings(
        tooling_root=Path(sys.prefix).resolve(),
        toolchain_interpreter=Path(sys.executable).resolve(),
        bridge_path=Path(codex.__file__).resolve().with_name("bridge.py"),
        # Preserve the live host's configured (or standard) account-state token
        # discovery path when no explicit credential file was supplied. The
        # private output-adjacent state remains appropriate for CI's source.
        state_root=(
            discovery_state_root if source is None else str(state_root)
        ),
        claude_cli_path=runtime if vendor == "claude" else None,
        codex_cli_path=runtime if vendor == "codex" else None,
        cursor_cli_path=runtime if vendor == "cursor" else None,
    )
    kwargs: dict[str, object] = {"requested_runtime_s": SEALED_RUNTIME_S}
    if source is not None:
        kwargs["credential_path"] = source
    if vendor == "claude":
        kwargs.update(
            claude_binary=runtime,
            sandbox_wrapper=wrapper,
            # Normal host discovery may deliberately use the independently
            # validated long-lived token source. Explicit CI credential files
            # remain OAuth-only and fail closed.
            allow_token_fallback=source is None,
        )
    elif vendor == "codex":
        kwargs["sandbox_wrapper"] = wrapper
    with settings.use(), (broker or _broker_for(vendor))(**kwargs) as descriptor:
        payload = _read_snapshot(descriptor)
        source_kind = (
            claude.snapshot_source(descriptor)
            if vendor == "claude"
            else f"{vendor}-auth-file"
        )
    _validate_access_only(vendor, payload)
    _write_snapshot(output, payload)
    return source_kind


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loopzero-credential-seal",
        description="Create an access-only native-runtime credential snapshot.",
    )
    parser.add_argument("vendor", choices=VENDORS)
    parser.add_argument(
        "--source",
        type=Path,
        help="explicit mode-0600 source; omit to use normal host discovery",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_kind = seal_credential(
            args.vendor, source=args.source, output=args.out
        )
    except Exception:
        # Errors are deliberately content-free: neither provider exceptions nor
        # path values can accidentally echo credential material or locations.
        print("credential sealing failed", file=sys.stderr)
        return 1
    print(f"credential source: {source_kind}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
