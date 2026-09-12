"""Single trusted ``gh`` CLI boundary for GitHub mechanisms."""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..config import Profile
from ..kernel import settings as kernel_settings
from ..kernel.git_config_security import (
    origin_url as git_config_origin_url,
    security_projection_sha256,
    validated_git_config_entries,
)
from ..kernel.gitscope import trusted_git_command
from ..trust import (
    TrustedExecutableError,
    allowed_path,
    git_environment,
    resolve_executable,
    system_executable,
    trusted_subprocess_environment,
)


class GitHubError(RuntimeError):
    pass


class GateError(RuntimeError):
    """A repository or network command crossed its pinned authority."""


class CommandTimeoutError(GateError):
    """A bounded GitHub observation exceeded its timeout."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CANONICAL_GITHUB_ORIGIN_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?"
)
UNSAFE_NETWORK_ENVIRONMENT_NAMES = frozenset(
    {
        "BASH_ENV", "CDPATH", "CURL_CA_BUNDLE", "ENV", "GH_REPO",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_ASKPASS", "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
        "GIT_CURL_VERBOSE", "GIT_DIR", "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF",
        "GIT_INDEX_FILE", "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY", "GIT_PROXY_COMMAND",
        "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_SSL_CERT", "GIT_SSL_CERT_PASSWORD_PROTECTED",
        "GIT_SSL_KEY", "GIT_SSL_NO_VERIFY", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_TRACE",
        "GIT_TRACE_CURL", "GIT_TRACE_CURL_NO_DATA", "GIT_TRACE_PACKET", "GIT_WORK_TREE",
        "HTTP_PROXY", "HTTPS_PROXY", "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP",
        "SSL_CERT_DIR", "SSL_CERT_FILE", "SSH_ASKPASS", "ALL_PROXY", "NO_PROXY",
        "all_proxy", "http_proxy", "https_proxy", "no_proxy",
    }
)


def github_repo_from_origin(origin_url: str) -> str:
    if CANONICAL_GITHUB_ORIGIN_RE.fullmatch(origin_url) is None:
        raise GateError("pinned origin is not a canonical GitHub HTTPS URL")
    repository = origin_url.removeprefix("https://github.com/").removesuffix(".git")
    if repository.count("/") != 1:
        raise GateError("pinned origin does not identify one GitHub repository")
    return f"github.com/{repository}"


class SecureGitRunner:
    """Repository-bound Git/GitHub runner moved from publication admission."""

    def __init__(
        self,
        repo: Path,
        *,
        origin_url: str | None = None,
        git_config_sha256: str | None = None,
        authority_repo: Path | None = None,
    ) -> None:
        if authority_repo is not None and authority_repo.is_symlink():
            raise GateError("pinned primary repository cannot be a symlink")
        self.repo = repo.resolve()
        self.origin_url = origin_url
        self.git_config_sha256 = git_config_sha256
        self.authority_repo = authority_repo.resolve() if authority_repo else None
        self.git_dir = (
            self._git_dir_from_authority(self.authority_repo)
            if self.authority_repo is not None else None
        )

    def _git_dir_from_authority(self, authority_repo: Path) -> Path:
        common_dir = authority_repo / ".git"
        if common_dir.is_symlink() or not common_dir.is_dir():
            raise GateError("pinned primary Git authority is unavailable")
        if self.repo == authority_repo:
            return common_dir
        registrations = common_dir / "worktrees"
        if registrations.is_symlink() or not registrations.is_dir():
            raise GateError("pinned worktree Git authority is unavailable")
        matches: list[Path] = []
        for candidate in registrations.iterdir():
            marker = candidate / "gitdir"
            if candidate.is_symlink() or not candidate.is_dir() or marker.is_symlink():
                continue
            try:
                registered = Path(marker.read_text(encoding="utf-8").strip())
            except OSError:
                continue
            if registered.is_absolute() and registered.name == ".git" and registered.parent.resolve() == self.repo:
                matches.append(candidate.resolve())
        if len(matches) != 1:
            raise GateError("pinned worktree Git authority is not unique")
        commondir_marker = matches[0] / "commondir"
        if commondir_marker.is_symlink() or not commondir_marker.is_file():
            raise GateError("pinned worktree common Git authority is unavailable")
        try:
            raw_common = Path(commondir_marker.read_text(encoding="utf-8").strip())
        except OSError as exc:
            raise GateError("pinned worktree common Git authority is unreadable") from exc
        registered_common = (raw_common if raw_common.is_absolute() else commondir_marker.parent / raw_common).resolve()
        if registered_common != common_dir.resolve():
            raise GateError("pinned worktree common Git authority changed")
        return matches[0]

    @staticmethod
    def _trusted_executable(name: str) -> str:
        try:
            return str(system_executable(name))
        except TrustedExecutableError as exc:
            raise GateError(str(exc)) from exc

    def _git_config_path(self) -> Path:
        if self.authority_repo is not None:
            return self.authority_repo / ".git" / "config"
        dot_git = self.repo / ".git"
        if dot_git.is_dir():
            return dot_git / "config"
        if dot_git.is_symlink() or not dot_git.is_file():
            raise GateError("pinned Git configuration is unavailable")
        try:
            marker = dot_git.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GateError("linked-worktree Git metadata cannot be read") from exc
        if not marker.startswith("gitdir: "):
            raise GateError("linked-worktree Git metadata is invalid")
        git_dir = Path(marker.removeprefix("gitdir: "))
        if not git_dir.is_absolute():
            git_dir = dot_git.parent / git_dir
        marker = git_dir.resolve() / "commondir"
        try:
            common_dir = Path(marker.read_text(encoding="utf-8").strip())
        except OSError as exc:
            raise GateError("linked-worktree common Git directory is unavailable") from exc
        if not common_dir.is_absolute():
            common_dir = marker.parent / common_dir
        return common_dir.resolve() / "config"

    @staticmethod
    def _validated_config_entries(raw_config: bytes) -> tuple[tuple[str, str | None], ...]:
        try:
            return validated_git_config_entries(raw_config)
        except ValueError as exc:
            if "unmeasured" in str(exc):
                raise GateError(
                    "canonical Git configuration uses unmeasured execution, redirect, "
                    "include, URL rewrite, or worktree configuration"
                ) from exc
            raise GateError("canonical Git configuration cannot be parsed") from exc

    def _ensure_github_binding(self) -> None:
        if bool(self.origin_url) != bool(self.git_config_sha256):
            raise GateError("GitHub repository binding must include origin and config digest")
        if self.origin_url is not None:
            return
        config = self._git_config_path()
        if config.is_symlink() or not config.is_file():
            raise GateError("pinned Git configuration is unavailable")
        try:
            raw = config.read_bytes()
            self._validated_config_entries(raw)
            origin = git_config_origin_url(raw)
        except (OSError, ValueError) as exc:
            raise GateError("canonical origin cannot be derived from Git config") from exc
        if CANONICAL_GITHUB_ORIGIN_RE.fullmatch(origin) is None:
            raise GateError("derived origin is not a canonical GitHub HTTPS URL")
        self.origin_url = origin
        self.git_config_sha256 = security_projection_sha256(raw)

    def _assert_git_config_binding(self) -> None:
        if self.git_config_sha256 is None:
            return
        config = self._git_config_path()
        if config.is_symlink() or not config.is_file():
            raise GateError("pinned Git configuration is unavailable")
        try:
            raw = config.read_bytes()
        except OSError as exc:
            raise GateError("pinned Git configuration cannot be read") from exc
        self._validated_config_entries(raw)
        if security_projection_sha256(raw) != self.git_config_sha256:
            raise GateError("pinned Git configuration changed before network access")

    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        input_text: str | None = None,
        preserve_output_bytes: bool = False,
    ) -> CommandResult:
        if preserve_output_bytes and input_text is not None:
            raise GateError("byte-preserving commands cannot accept text input")
        git_command = bool(args) and args[0] == "git"
        git_network = len(args) >= 2 and args[0] == "git" and args[1] in {"fetch", "ls-remote", "push"}
        gh_network = bool(args) and args[0] == "gh"
        command = list(args)
        if git_command or gh_network:
            command[0] = self._trusted_executable(args[0])
        if git_network or gh_network or (git_command and self.git_config_sha256 is not None):
            self._ensure_github_binding()
            self._assert_git_config_binding()
        command_env = env
        if git_command or gh_network:
            command_env = trusted_subprocess_environment(os.environ if env is None else env)
            inherited_host = command_env.get("GH_HOST")
            safe = {
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            }
            if self.git_dir is not None:
                safe.update({"GIT_DIR": str(self.git_dir), "GIT_WORK_TREE": str(self.repo)})
            if git_network or gh_network:
                gh = self._trusted_executable("gh")
                safe.update({
                    "GIT_CONFIG_COUNT": "5",
                    "GIT_CONFIG_KEY_1": "credential.https://github.com.helper", "GIT_CONFIG_VALUE_1": "",
                    "GIT_CONFIG_KEY_2": "credential.https://github.com.helper", "GIT_CONFIG_VALUE_2": f"!{gh} auth git-credential",
                    "GIT_CONFIG_KEY_3": "credential.https://gist.github.com.helper", "GIT_CONFIG_VALUE_3": "",
                    "GIT_CONFIG_KEY_4": "credential.https://gist.github.com.helper", "GIT_CONFIG_VALUE_4": f"!{gh} auth git-credential",
                })
            unsafe = [key for key, value in command_env.items() if (key.startswith("GIT_CONFIG_") or key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}) and safe.get(key) != value]
            if unsafe:
                raise GateError("Git environment contains unmeasured overrides: " + ", ".join(sorted(unsafe)))
            for name in UNSAFE_NETWORK_ENVIRONMENT_NAMES:
                command_env.pop(name, None)
            command_env.update(safe)
        if gh_network:
            assert self.origin_url is not None and command_env is not None
            if inherited_host not in {None, "", "github.com"}:
                raise GateError(f"GH_HOST conflicts with pinned github.com origin: {inherited_host}")
            command_env["GH_HOST"] = "github.com"
            command_env["GH_REPO"] = github_repo_from_origin(self.origin_url)
        try:
            completed = subprocess.run(
                command, cwd=self.repo, check=False, capture_output=True,
                input=input_text, text=not preserve_output_bytes, env=command_env,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise CommandTimeoutError(
                f"`{' '.join(args)}` timed out after {timeout_seconds:g}s"
            ) from exc
        stdout = os.fsdecode(completed.stdout) if preserve_output_bytes else completed.stdout
        stderr = os.fsdecode(completed.stderr) if preserve_output_bytes else completed.stderr
        result = CommandResult(completed.returncode, stdout, stderr)
        if check and result.returncode:
            raise GateError(
                f"`{' '.join(args)}` failed with exit {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result


@dataclass(frozen=True, slots=True)
class Repository:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class GitHubSettings:
    labels: Mapping[str, str]
    host: str = "github.com"
    token_environment: str = "GH_TOKEN"
    ref_namespace: str = "refs/heads"
    version_floor: tuple[int, int, int] = (2, 40, 0)
    retries: int = 2
    trusted_bin_dir: Path = Path("/usr/bin")

    @classmethod
    def from_profile(cls, profile: Profile) -> "GitHubSettings":
        trusted = Path(profile.toolchain.get("trusted_bin_dir", "/usr/bin"))
        return cls(
            labels=dict(profile.github.labels),
            host=getattr(profile.github, "host", "github.com"),
            token_environment=getattr(
                profile.github, "token_environment", "GH_TOKEN"
            ),
            ref_namespace=profile.github.ref_namespace,
            version_floor=profile.github.gh_version_floor,
            retries=profile.github.retries,
            trusted_bin_dir=trusted,
        )


_ORIGIN_SCP_RE = re.compile(
    r"^(?:[^@/]+@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>.+)$"
)
_VERSION_RE = re.compile(r"\bgh version (\d+)\.(\d+)\.(\d+)\b")
_HOST_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)
_GH_CHILD_ENVIRONMENT = frozenset(
    {"LANG", "LANGUAGE", "NO_COLOR", "PATH", "TERM", "TZ"}
)
_GH_HOME_DIRECTORY = "gh-home"


def _account_state_root() -> Path:
    """Resolve the OS account state root from passwd, never from ``$HOME``."""
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise GitHubError("OS account home is unavailable") from exc
    if not home.is_absolute() or home == Path("/"):
        raise GitHubError("OS account home is unsafe")
    return kernel_settings.settings.account_state_root(home)


def _private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise GitHubError("private gh home is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise GitHubError("private gh home is unsafe")
    return path


@contextmanager
def private_gh_home() -> Iterator[Path]:
    """Yield a fresh, empty, mode-0700 directory that lives for one gh command.

    gh resolves its configuration directory through ``GH_CONFIG_DIR``,
    ``XDG_CONFIG_HOME`` and then ``$HOME/.config/gh``.  With ``HOME`` unset
    that last fallback becomes the *relative* path ``.config/gh`` under the
    child's cwd, which is the candidate checkout.  Repository content could
    then plant ``config.yml`` (``http_unix_socket`` transport redirects) or
    ``hosts.yml`` (an attacker-chosen ``oauth_token``).  Pointing both
    ``GH_CONFIG_DIR`` and ``HOME`` at a private empty directory under the OS
    account state root closes every gh config, state and data lookup at once.
    """
    parent = _private_directory(_account_state_root() / _GH_HOME_DIRECTORY)
    try:
        home = Path(tempfile.mkdtemp(prefix="gh-", dir=parent))
    except OSError as exc:
        raise GitHubError("private gh home is unavailable") from exc
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


def repository_from_origin(
    origin: str, *, github_host: str = "github.com"
) -> Repository:
    """Parse an HTTPS, SSH URL or scp-like Git origin without contacting GitHub."""
    value = origin.strip()
    configured_host = github_host.strip().lower()
    if _HOST_RE.fullmatch(configured_host) is None:
        raise GitHubError("configured GitHub host is invalid")
    match = _ORIGIN_SCP_RE.fullmatch(value)
    if match and "://" not in value:
        origin_host = match.group("host").lower()
        path = match.group("path")
    else:
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.hostname is None
            or parsed.port is not None
        ):
            raise GitHubError("origin is not a supported GitHub URL")
        origin_host = parsed.hostname.lower()
        path = parsed.path.lstrip("/")
    if origin_host != configured_host:
        raise GitHubError(
            f"origin host {origin_host!r} does not match configured GitHub host "
            f"{configured_host!r}"
        )
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
        raise GitHubError("origin does not identify one owner/repository")
    return Repository(*parts)


class GitHub:
    def __init__(
        self,
        root: Path,
        settings: GitHubSettings,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = root.resolve()
        self.settings = settings
        if _HOST_RE.fullmatch(settings.host) is None or settings.host != settings.host.lower():
            raise GitHubError("configured GitHub host is invalid")
        if settings.token_environment not in {"GH_TOKEN", "GITHUB_TOKEN"}:
            raise GitHubError("configured GitHub token environment is invalid")
        self._run = run
        self._sleep = sleep
        executable = resolve_executable(
            self.root, "gh", allowed_path((str(settings.trusted_bin_dir),))
        )
        if executable is None:
            raise GitHubError("trusted gh executable is unavailable")
        self.executable = executable
        self._checked_version = False
        self._repository: Repository | None = None

    def _child_environment(self, home: Path) -> dict[str, str]:
        """Pass only non-authoritative process context and one selected token.

        ``home`` must come from :func:`private_gh_home`; it is exported as both
        ``HOME`` and ``GH_CONFIG_DIR`` so gh never consults a cwd-relative or
        caller-selected configuration directory.
        """
        if not home.is_absolute():
            raise GitHubError("private gh home must be absolute")
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in _GH_CHILD_ENVIRONMENT or name.startswith("LC_")
        }
        environment["HOME"] = str(home)
        environment["GH_CONFIG_DIR"] = str(home)
        token = os.environ.get(self.settings.token_environment)
        if token:
            environment[self.settings.token_environment] = token
        return environment

    def _command(self, argv: Sequence[str], *, input: str | None = None) -> str:
        last = ""
        retryable = True
        if argv and argv[0] == "api":
            try:
                method = argv[argv.index("--method") + 1].upper()
            except (ValueError, IndexError):
                method = "GET"
            retryable = method in {"GET", "HEAD"}
        attempts = self.settings.retries + 1 if retryable else 1
        command = list(argv)
        if command and command[0] == "api":
            command[1:1] = ["--hostname", self.settings.host]
        for attempt in range(attempts):
            with private_gh_home() as home:
                proc = self._run(
                    [str(self.executable), *command],
                    cwd=self.root,
                    env=self._child_environment(home),
                    input=input, text=True, capture_output=True,
                )
            if proc.returncode == 0:
                return proc.stdout
            last = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            if attempt + 1 < attempts:
                self._sleep(min(2**attempt, 4))
        raise GitHubError(f"gh {' '.join(argv[:2])} failed: {last}")

    def check_version(self) -> tuple[int, int, int]:
        output = self._command(("version",))
        match = _VERSION_RE.search(output)
        if match is None:
            raise GitHubError("gh version output is malformed")
        version = tuple(map(int, match.groups()))
        if version < self.settings.version_floor:
            floor = ".".join(map(str, self.settings.version_floor))
            raise GitHubError(f"gh {floor} or newer is required")
        self._checked_version = True
        return version  # type: ignore[return-value]

    def repository(self) -> Repository:
        if self._repository is None:
            proc = subprocess.run(
                trusted_git_command(self.root, "remote", "get-url", "origin"), cwd=self.root,
                env=git_environment(), text=True, capture_output=True,
            )
            if proc.returncode:
                raise GitHubError("origin remote is unavailable")
            self._repository = repository_from_origin(
                proc.stdout, github_host=self.settings.host
            )
        return self._repository

    def api(
        self, endpoint: str, *, method: str = "GET", fields: Mapping[str, object] | None = None
    ) -> object:
        if not self._checked_version:
            self.check_version()
        if endpoint.startswith("/") or ".." in endpoint.split("/"):
            raise GitHubError("GitHub endpoint must be relative and normalized")
        argv = ["api", "--method", method, endpoint]
        payload = None
        if fields is not None:
            argv.extend(("--input", "-"))
            payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        output = self._command(argv, input=payload)
        try:
            return json.loads(output) if output.strip() else None
        except json.JSONDecodeError as exc:
            raise GitHubError("gh returned malformed JSON") from exc

    def labels(self, names: Sequence[str]) -> tuple[str, ...]:
        try:
            return tuple(sorted({self.settings.labels[name] for name in names}))
        except KeyError as exc:
            raise GitHubError(f"unknown configured label key {exc.args[0]!r}") from exc

    def ref(self, branch: str) -> str:
        if not branch or branch.startswith("/") or ".." in branch.split("/"):
            raise GitHubError("branch name is invalid")
        return f"{self.settings.ref_namespace}/{branch}"


__all__ = [
    "GitHub",
    "GitHubError",
    "GitHubSettings",
    "Repository",
    "private_gh_home",
    "repository_from_origin",
]
