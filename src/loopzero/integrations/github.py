"""Single trusted ``gh`` CLI boundary for GitHub mechanisms."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..config import Profile
from ..kernel.gitscope import trusted_git_command
from ..trust import allowed_path, git_environment, resolve_executable


class GitHubError(RuntimeError):
    pass


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
    ref_namespace: str = "refs/heads"
    version_floor: tuple[int, int, int] = (2, 40, 0)
    retries: int = 2
    trusted_bin_dir: Path = Path("/usr/bin")

    @classmethod
    def from_profile(cls, profile: Profile) -> "GitHubSettings":
        trusted = Path(profile.toolchain.get("trusted_bin_dir", "/usr/bin"))
        return cls(
            labels=dict(profile.github.labels),
            ref_namespace=profile.github.ref_namespace,
            version_floor=profile.github.gh_version_floor,
            retries=profile.github.retries,
            trusted_bin_dir=trusted,
        )


_ORIGIN_SCP_RE = re.compile(r"^(?:[^@/]+@)?[^:/]+:(?P<path>.+)$")
_VERSION_RE = re.compile(r"\bgh version (\d+)\.(\d+)\.(\d+)\b")


def repository_from_origin(origin: str) -> Repository:
    """Parse an HTTPS, SSH URL or scp-like Git origin without contacting GitHub."""
    value = origin.strip()
    match = _ORIGIN_SCP_RE.fullmatch(value)
    if match and "://" not in value:
        path = match.group("path")
    else:
        parsed = urlparse(value)
        path = parsed.path.lstrip("/")
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

    def _command(self, argv: Sequence[str], *, input: str | None = None) -> str:
        last = ""
        for attempt in range(self.settings.retries + 1):
            proc = self._run(
                [str(self.executable), *argv], cwd=self.root, env=git_environment(),
                input=input, text=True, capture_output=True,
            )
            if proc.returncode == 0:
                return proc.stdout
            last = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            if attempt < self.settings.retries:
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
            self._repository = repository_from_origin(proc.stdout)
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
    "GitHub", "GitHubError", "GitHubSettings", "Repository", "repository_from_origin"
]
