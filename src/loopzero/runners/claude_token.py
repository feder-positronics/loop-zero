"""Host-only validation and sealed snapshots of inference-only Claude tokens."""

from .settings import DEFAULT_SETTINGS, RuntimeSettings, get_settings, using_adapter_settings


import http.client
import json
import os
import pwd
import re
from pathlib import Path

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
TOKEN_FILE_ENV = DEFAULT_SETTINGS.env_name("CLAUDE_TOKEN_FILE")
TOKEN_PATTERN = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{40,512}")


def _validate_remote_token(token: str) -> None:
    """Authenticate inference-only tokens using Claude Code's token-count probe.

    Setup tokens lack the profile scopes required by /api/oauth/validate.
    Pin TLS origin, ignore proxies, refuse redirects, and discard response text.
    Counting a fixed input authenticates without generating a model response.
    """
    from .claude_credential import ClaudeCredentialUnavailable

    connection = http.client.HTTPSConnection("api.anthropic.com", timeout=10)
    valid = False
    try:
        connection.request(
            "POST", "/v1/messages/count_tokens?beta=true",
            body=json.dumps({
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "."}],
            }).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20,token-counting-2024-11-01",
            },
        )
        response = connection.getresponse()
        if 200 <= response.status < 300:
            payload = response.read(16385)
            if len(payload) <= 16384:
                decoded = json.loads(payload)
                if isinstance(decoded, dict):
                    count = decoded.get("input_tokens")
                    valid = type(count) is int and count >= 0
    except (OSError, http.client.HTTPException, ValueError, TypeError, AttributeError):
        pass
    finally:
        connection.close()
    if not valid:
        raise ClaudeCredentialUnavailable("Claude long-lived token validation failed")


def _read_host_token_file(path: Path) -> bytes:
    """Walk pinned directories without following aliases or entering a repo."""
    from .claude_credential import (
        ClaudeCredentialUnavailable, UnsafeClaudeCredential, _read_private_payload,
    )

    if not path.is_absolute() or ".." in path.parts:
        raise UnsafeClaudeCredential("Claude token file path is invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open("/", flags)
    try:
        for component in (*path.parts[1:-1], None):
            try:
                os.stat(".git", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise UnsafeClaudeCredential("Claude token file must be outside repositories")
            if component is not None:
                child = os.open(component, flags, dir_fd=directory)
                os.close(directory)
                directory = child
        return _read_private_payload(
            Path(path.name), directory_fd=directory, single_link=True
        )
    except FileNotFoundError as exc:
        raise ClaudeCredentialUnavailable("Claude token file is unavailable") from exc
    except OSError:
        raise UnsafeClaudeCredential("Claude token file path is unsafe") from None
    finally:
        os.close(directory)


def _default_token_path() -> Path:
    """Use the OS account state root, independent of desktop HOME/XDG state."""
    from .claude_credential import ClaudeCredentialUnavailable, UnsafeClaudeCredential

    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        raise ClaudeCredentialUnavailable("Claude account home is unavailable") from None
    if not home.is_absolute() or home == Path("/"):
        raise UnsafeClaudeCredential("Claude account home is unsafe")
    return get_settings().state_path(home) / "claude-token"


def token_snapshot() -> int | None:
    """Read only host-selected sources; never install a token in durable state."""
    from .claude_credential import (
        ClaudeCredentialUnavailable, UnsafeClaudeCredential,
        _snapshot_descriptor,
    )

    path = os.environ.get(get_settings().env_name("CLAUDE_TOKEN_FILE"))
    source = "token-file"
    if path is None and TOKEN_ENV not in os.environ:
        path = str(_default_token_path())
        source = "token-file(default)"
    if path is not None:
        if not path or not Path(path).is_absolute():
            raise UnsafeClaudeCredential("Claude token file path is invalid")
        try:
            token = _read_host_token_file(Path(path)).decode("ascii").removesuffix("\n")
        except ClaudeCredentialUnavailable as exc:
            if source == "token-file(default)" and isinstance(exc.__cause__, FileNotFoundError):
                return None
            raise
        except UnicodeDecodeError:
            raise UnsafeClaudeCredential("Claude long-lived token is malformed") from None
    else:
        token = os.environ.get(TOKEN_ENV)
        source = "token-env"
    if token is None:
        raise ClaudeCredentialUnavailable("Claude long-lived token is unavailable")
    if TOKEN_PATTERN.fullmatch(token) is None:
        raise UnsafeClaudeCredential("Claude long-lived token is malformed")
    _validate_remote_token(token)
    return _snapshot_descriptor(json.dumps(
        {"claudeCodeOauthToken": token, "source": source}, separators=(",", ":")
    ).encode())


def _snapshot(fd: int) -> dict:
    from .claude_credential import MAX_CREDENTIAL_BYTES, UnsafeClaudeCredential

    try:
        decoded = json.loads(os.pread(fd, MAX_CREDENTIAL_BYTES + 1, 0))
    except (OSError, ValueError):
        raise UnsafeClaudeCredential("Claude snapshot is invalid") from None
    if not isinstance(decoded, dict):
        raise UnsafeClaudeCredential("Claude snapshot is invalid")
    return decoded


def snapshot_token(fd: int) -> str | None:
    from .claude_credential import UnsafeClaudeCredential

    decoded = _snapshot(fd)
    if "claudeCodeOauthToken" not in decoded:
        return None
    token = decoded["claudeCodeOauthToken"]
    if (not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None
            or decoded.get("source") not in {"token-env", "token-file", "token-file(default)"}):
        raise UnsafeClaudeCredential("Claude token snapshot is invalid")
    return token


def snapshot_source(fd: int) -> str:
    if snapshot_token(fd) is None:
        return "oauth-file"
    return _snapshot(fd)["source"]


def protected_token_available() -> bool:
    """Used only by host readiness; workers never receive this broker marker."""
    from .claude_credential import CLAUDE_AUTH_FD_ENV, ClaudeCredentialError

    try:
        return snapshot_token(int(os.environ[get_settings().env_name("CLAUDE_AUTH_FD")])) is not None
    except (KeyError, ValueError, OSError, ClaudeCredentialError):
        return False
