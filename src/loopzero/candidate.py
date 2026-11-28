"""Attest a live Mergify candidate around a caller-owned source policy.

The caller owns API reads and source eligibility. This module only binds an
authenticated temporary candidate to stable, current source PR heads without
checking out or executing candidate code.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from loopzero import github
from loopzero.types import LoopZeroError, is_sha

MERGIFY_APP_ID = 10562
MERGIFY_APP_OWNER = "Mergifyio"
MERGIFY_APP_URL = "https://github.com/apps/mergify"
MERGIFY_BOT_ID = 37929162
MERGIFY_BOT_LOGIN = "mergify[bot]"
QUEUE_BRANCH_PREFIX = "mergify/merge-queue/"

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ATTEST_SECONDS = 30
_ATTEST_DELAY = 4
_BATCH_SECONDS = 90
_retry_sleep = time.sleep
_retry_clock = time.monotonic
_TREE_SECONDS = 120
_TREE_BYTES = 256 * 1024 * 1024
_TREE_OUTPUT = 8 * 1024 * 1024
_TREE_OBJECTS = 100_000
_TREE_DEPTH = 200
# Apply the limit in a child interpreter before replacing it with Git.
_LIMITED_GIT = """\
import os
import resource
import sys

limit = int(sys.argv[1])
soft, _ = resource.getrlimit(resource.RLIMIT_FSIZE)
if soft != resource.RLIM_INFINITY:
    limit = min(limit, soft)
resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
os.execvp("git", sys.argv[2:])
"""
_FETCH_TRANSPORT_ENV = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "GIT_SSL_CAINFO")

JsonObject = dict[str, Any]
JsonValue = JsonObject | list[Any]
GitHubGet = Callable[[str], JsonValue]
MergifyStatus = Callable[[str, str, str], JsonObject]
SourcePolicy = Callable[[int, JsonObject], None]


class CandidateError(LoopZeroError):
    """Candidate identity, membership, history, or source policy failed closed."""


class CandidateBatchError(CandidateError):
    """The candidate has no unique live batch during a possible queue rebuild."""


class CandidateRaceError(CandidateError):
    """A complete attestation observed concurrent source, lineage or main drift."""


def _require(condition: bool, message: str, error: type[CandidateError] = CandidateError) -> None:
    if not condition:
        raise error(message)


def _field(value: object, name: str) -> object:
    return value.get(name) if isinstance(value, dict) else None


def _identity(actor: object) -> tuple[object, object, object, object]:
    if not isinstance(actor, dict):
        return (None, None, None, None)
    return actor.get("id"), actor.get("login"), actor.get("type"), actor.get("html_url")


def _object(value: object, message: str) -> JsonObject:
    _require(isinstance(value, dict), message)
    return value


def flatten_batches(status: object) -> dict[str, JsonObject]:
    batches = _object(status, "Mergify status is not an object").get("batches")
    _require(isinstance(batches, list), "Mergify status has no batches list")
    indexed: dict[str, JsonObject] = {}

    def visit(items: list[object]) -> None:
        for item in items:
            item = _object(item, "Mergify status contains an invalid batch")
            batch_id = item.get("id")
            _require(isinstance(batch_id, str) and batch_id, "Mergify batch has no id")
            _require(batch_id not in indexed, f"Mergify batch id is duplicated: {batch_id}")
            indexed[batch_id] = item
            children = item.get("sub_batches")
            if children is not None:
                _require(isinstance(children, list), f"Mergify batch {batch_id} has invalid sub_batches")
                visit(children)

    visit(batches)
    return indexed


def _candidate_lineage(status: JsonObject, candidate_number: int) -> tuple[str, list[int], list[str]]:
    indexed = flatten_batches(status)
    matches = [batch_id for batch_id, batch in indexed.items()
               if batch.get("queue_pull_request_number") == candidate_number]
    _require(len(matches) == 1, f"PR #{candidate_number} is not exactly one live Mergify batch",
             CandidateBatchError)
    candidate_id = matches[0]
    pending, visited = [candidate_id], set()
    source_numbers, missing = [], []
    while pending:
        batch_id = pending.pop()
        if batch_id in visited:
            continue
        batch = indexed.get(batch_id)
        visited.add(batch_id)
        if batch is None:
            missing.append(batch_id)
            continue
        pull_requests = batch.get("pull_requests")
        _require(isinstance(pull_requests, list), f"Mergify batch {batch_id} has no pull_requests list")
        for pull_request in pull_requests:
            number = _field(pull_request, "number")
            _require(isinstance(number, int) and number > 0,
                     f"Mergify batch {batch_id} has an invalid source pull request")
            source_numbers.append(number)
        parent_ids = batch.get("parent_ids", [])
        _require(isinstance(parent_ids, list)
                 and all(isinstance(parent, str) and parent for parent in parent_ids),
                 f"Mergify batch {batch_id} has invalid parent_ids")
        pending.extend(parent_ids)
    _require(source_numbers, f"Mergify batch for PR #{candidate_number} has no sources")
    _require(len(source_numbers) == len(set(source_numbers)),
             f"Mergify batch for PR #{candidate_number} repeats a source pull request")
    return candidate_id, sorted(source_numbers), sorted(missing)


def _repo_identity(repo: object) -> tuple[object, object]:
    return _field(repo, "id"), _field(repo, "full_name")


def _candidate_snapshot(
    candidate: JsonObject, number: int, event_head: JsonObject, repository: JsonObject,
    expected_identity: tuple[object, object, object, object],
    base_branch: str,
) -> None:
    _require(candidate.get("number") == number, "GitHub returned the wrong candidate PR")
    state = candidate.get("state")
    state_error = "Mergify candidate PR is superseded (closed)" if state == "closed" else "Mergify candidate PR is not open"
    _require(state == "open", state_error)
    _require(candidate.get("draft") is True, "Mergify candidate PR must be a draft")
    _require(_identity(candidate.get("user")) == expected_identity,
             "Candidate author is not the verified Mergify GitHub App")
    head, base = candidate.get("head"), candidate.get("base")
    _require(isinstance(head, dict) and isinstance(base, dict), "Candidate refs are missing")
    _require(head.get("sha") == event_head.get("sha"), "Candidate head changed after the workflow event")
    _require(head.get("ref") == event_head.get("ref"), "Mergify candidate has an unexpected head ref")
    _require(base.get("ref") == base_branch, f"Mergify candidate must target the real {base_branch} branch")
    expected_repo = _repo_identity(repository)
    for label, repo in (("head", head.get("repo")), ("base", base.get("repo"))):
        _require(isinstance(repo, dict) and _repo_identity(repo) == expected_repo,
                 f"Mergify candidate {label} repository does not match this repository")


def _source_fingerprint(source: JsonObject) -> tuple[object, ...]:
    base = source.get("base")
    base_repo = _field(base, "repo")
    labels = source.get("labels", [])
    if not isinstance(labels, list) or any(not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels):
        raise CandidateError("invalid source PR labels")
    return (source.get("number"), source.get("merged"), source.get("mergeable") is False,
            tuple(label["name"] for label in labels), source.get("state"), source.get("draft"), source.get("body"),
            _field(source.get("head"), "sha"), _field(base, "ref"), *_repo_identity(base_repo))


def _lookup_source(github_get: GitHubGet, repo: str, number: int) -> JsonObject:
    return _object(github_get(f"/repos/{repo}/pulls/{number}"),
                   f"GitHub source PR #{number} lookup returned an invalid payload")


def _attest_source(
    source: JsonObject, *, number: int, repository: JsonObject, candidate_sha: str,
    github_get: GitHubGet, base_branch: str,
) -> str:
    _require(source.get("number") == number, f"GitHub returned the wrong source PR for #{number}")
    _require(source.get("state") == "open", f"source PR #{number} is not open",
             CandidateRaceError if source.get("merged") is True else CandidateError)
    head, base = source.get("head"), source.get("base")
    _require(isinstance(head, dict) and isinstance(base, dict), f"source PR #{number} refs are missing")
    base_repo = base.get("repo")
    _require(base.get("ref") == base_branch and isinstance(base_repo, dict)
             and _repo_identity(base_repo) == _repo_identity(repository),
             f"source PR #{number} does not target this repository's {base_branch} branch")
    source_sha = head.get("sha")
    _require(is_sha(source_sha), f"source PR #{number} has an invalid head")
    comparison_path = f"/repos/{repository['full_name']}/compare/{source_sha}...{candidate_sha}"
    comparison = _object(github_get(comparison_path), "GitHub compare returned an invalid payload")
    _require(comparison.get("status") in {"ahead", "identical"}
             and _field(comparison.get("merge_base_commit"), "sha") == source_sha,
             f"source PR #{number} head {source_sha} is not an ancestor of the candidate")
    return source_sha


class FetchFailed(CandidateError):
    """The Git object fetch itself failed, as opposed to the history lacking evidence."""


class _MissingMergeHistory(CandidateError):
    """Merge evidence is missing or incomplete in the current Git store."""


_FETCH_TRANSPORT = re.compile(
    r"returned error: (?:429|5\d\d)|could not resolve (?:host|proxy)|failed to connect to|connection (?:refused|timed out)|"
    r"early eof|unexpected disconnect|empty reply from server|rpc failed; (?:curl (?:18|28|52|55|56|92)|http (?:5\d\d|429))|"
    r"gnutls_handshake|gnutls recv error|ssl_(?:read|connect)|openssl ssl_|ssl connection|tls handshake|"
    r"transfer closed|received http code 5\d\d from proxy|network is unreachable", re.IGNORECASE)
_FETCH_DENIED = re.compile(r"returned error: 4(?!29)\d\d|rpc failed; http 4(?!29)\d\d", re.IGNORECASE)


def _require_time(args: tuple[str, ...], deadline: float) -> None:
    if time.monotonic() >= deadline:
        if args[0] == "fetch" and f"--depth={_TREE_DEPTH}" in args:
            # Deepening can exhaust the shared history budget.
            raise github.GhUnavailable(("git", "fetch"), "GitHub objects temporarily unavailable")
        raise CandidateError("Git time bound exceeded")


def _require_git_exit(operation: str, returncode: int, output: BinaryIO) -> None:
    if operation == "merge-base" and returncode == 1:
        raise _MissingMergeHistory("Git history does not prove the requested merge base or ancestry")
    if operation == "fetch" and returncode:
        output.seek(0)
        diagnostic = output.read(_TREE_OUTPUT).decode(errors="replace")
        diagnostic = re.sub(r"https?://\S+", "<url>", diagnostic)
        if not _FETCH_DENIED.search(diagnostic) and (
            github._transient_error(diagnostic) or _FETCH_TRANSPORT.search(diagnostic)
        ):
            raise github.GhUnavailable(("git", "fetch"), "GitHub objects temporarily unavailable")
    cause = ("objects unavailable or a resource bound was hit" if operation == "fetch"
             else "objects unavailable, resource bound hit, or integration conflict")
    _require(returncode == 0, f"git {operation} exited {returncode}: {cause}",
             FetchFailed if operation == "fetch" else CandidateError)


def _fetch_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        try:
            token = github.auth_token()
        except github.GhUnavailable:
            raise
        except github.GhError:
            raise CandidateError("GitHub fetch authentication unavailable") from None
    _require(bool(token), "GitHub fetch authentication unavailable")
    return token


def _fetch_history(run: Callable[..., str], repo: str, heads: list[str],
                   env: dict[str, str], depth: str) -> None:
    run("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
        depth, f"https://github.com/{repo}.git", *heads, command_env=env)
    objects = run("rev-list", "--objects", "--no-object-names", *heads).splitlines()
    _require(len(objects) <= _TREE_OBJECTS, "Git object count bound exceeded")


def _merge_history_git(run: Callable[..., str], fetch: Callable[[str], None], shallow: Path) -> Callable[..., str]:
    increment = _TREE_DEPTH

    def git(*args: str, **kwargs: Any) -> str:
        nonlocal increment
        while True:
            try:
                result = run(*args, **kwargs)
                if args[:2] == ("merge-base", "--all") and shallow.exists() and run(
                    "rev-list", "--max-parents=0", *args[2:], "--not", *result.splitlines(),
                ):
                    raise _MissingMergeHistory("shallow history may hide other merge bases")
                return result
            except _MissingMergeHistory:
                if not shallow.exists():
                    raise
                boundary = set(shallow.read_bytes().splitlines())
                fetch(f"--deepen={increment}")
                if shallow.exists() and set(shallow.read_bytes().splitlines()) == boundary:
                    raise
                increment *= 2

    return git


@contextlib.contextmanager
def fetched_git(repo: str, heads: list[str]) -> Iterator[Callable[..., str]]:
    """Run bounded Git commands on fetched objects in an isolated bare repository."""
    token = _fetch_token()
    with tempfile.TemporaryDirectory(prefix="loopzero-candidate-") as directory:
        root = Path(directory)
        env = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": directory,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_AUTHOR_NAME": "loopzero", "GIT_AUTHOR_EMAIL": "loopzero@localhost",
            "GIT_COMMITTER_NAME": "loopzero", "GIT_COMMITTER_EMAIL": "loopzero@localhost",
        }
        fetch_env = env | {name: os.environ[name] for name in _FETCH_TRANSPORT_ENV if name in os.environ} | {
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic " + base64.b64encode(
                f"x-access-token:{token}".encode()).decode(),
        }
        deadline = time.monotonic() + _TREE_SECONDS

        def run(*args: str, command_env: dict[str, str] = env, preserve_newlines: bool = False,
                merge_conflict_status: bool = False) -> str:
            with (root / "output").open("w+b") as output:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-c", _LIMITED_GIT, str(_TREE_BYTES),
                     "git", "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0",
                     "-c", "maintenance.auto=false", *args],
                    cwd=root, env=command_env, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=output if args[0] == "fetch" else subprocess.DEVNULL, start_new_session=True,
                )
                def store_size() -> int:  # Git renames temporaries: skip files that vanish
                    total = 0
                    for folder, _, names in os.walk(root):
                        for name in names:
                            with contextlib.suppress(FileNotFoundError):
                                total += os.lstat(os.path.join(folder, name)).st_size
                    return total

                try:
                    while process.poll() is None:
                        _require(store_size() <= _TREE_BYTES, "Git object/size bound exceeded")
                        _require_time(args, deadline)
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            process.wait(timeout=0.02)
                    report_status = merge_conflict_status and args[0] == "merge-tree" and process.returncode in {0, 1}
                    if not report_status:
                        _require_git_exit(args[0], process.returncode, output)
                    _require(store_size() <= _TREE_BYTES, "Git object/size bound exceeded")
                    output.seek(0)
                    result = output.read(_TREE_OUTPUT + 1)
                    _require(len(result) <= _TREE_OUTPUT, "Git object/output bound exceeded")
                    if report_status:
                        return str(process.returncode)
                    if preserve_newlines:
                        return result.decode("utf-8", errors="surrogateescape")
                    return result.decode().strip()
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()

        fetch = partial(_fetch_history, run, repo, heads, fetch_env)
        run("init", "--bare", "--template=", ".")
        fetch(f"--depth={_TREE_DEPTH}")
        yield _merge_history_git(run, fetch, root / "shallow")


def _verify_tree(repo: str, main: str, candidate: str, sources: list[str]) -> None:
    """Reconstruct only first-parent, two-parent integrations; never execute objects."""
    _require(len(sources) <= 32 and len(set(sources)) == len(sources),
             "ambiguous or oversized source set")
    with fetched_git(repo, [main, candidate, *sources]) as git:
        graph = {row[0]: row[1:] for line in git(
            "rev-list", "--parents", candidate).splitlines() if (row := line.split())}
        pending, reverse_order = set(sources), []
        cursor = candidate
        while cursor in graph and pending:
            parents = graph[cursor]
            _require(len(parents) <= 2, "ambiguous candidate integration topology")
            for source in (cursor, *parents[1:]):
                if source in pending:
                    pending.remove(source)
                    reverse_order.append(source)
            cursor = parents[0] if parents else ""
        _require(not pending, "ambiguous source order in candidate topology")
        integrated = main
        for source in reversed(reverse_order):
            git("merge-base", "--all", integrated, source)
            tree = git("merge-tree", "--write-tree", integrated, source)
            _require(is_sha(tree), "invalid integration tree")
            integrated = git("commit-tree", tree, "-p", integrated, "-p", source,
                             "-m", "Candidate tree attestation")
        _require(git("rev-parse", f"{integrated}^{{tree}}") ==
                 git("rev-parse", f"{candidate}^{{tree}}"), "candidate tree mismatch")


def attest_candidate(
    event: JsonObject, *, base_branch: str = "main", github_get: GitHubGet,
    mergify_status: MergifyStatus, source_policy: SourcePolicy,
) -> list[dict[str, object]]:
    """Retry races afresh, bounded by retry admission rather than synchronous I/O.

    Source policy must raise CandidateError for an expected rejection.
    """
    batch_wait = [0.0]
    batch_deadline = _retry_clock() + _BATCH_SECONDS
    deadline = _retry_clock() + _ATTEST_SECONDS
    while True:
        try:
            return _attest_candidate_once(
                event, base_branch, github_get=github_get, mergify_status=mergify_status,
                source_policy=source_policy, batch_deadline=batch_deadline, batch_wait=batch_wait,
            )
        except CandidateRaceError:
            race_deadline = min(batch_deadline, deadline + batch_wait[0])
            if _retry_clock() + _ATTEST_DELAY >= race_deadline:
                raise
            _retry_sleep(_ATTEST_DELAY)
            if _retry_clock() >= race_deadline:
                raise


def _attest_candidate_once(  # noqa: C901 - candidate retry loop with one outcome per error
    event: JsonObject, base_branch: str, *, github_get: GitHubGet, mergify_status: MergifyStatus,
    source_policy: SourcePolicy, batch_deadline: float, batch_wait: list[float],
) -> list[dict[str, object]]:
    """Return stable source evidence after every source passes `source_policy`."""
    repository = _object(event.get("repository"), "GitHub event repository is missing")
    event_pr = _object(event.get("pull_request"), "GitHub event pull request is missing")
    _require(event.get("action") in {"opened", "synchronize"},
             "Candidate attestation requires its creation or head-update event")
    full_name = repository.get("full_name")
    _require(isinstance(full_name, str) and _REPOSITORY.fullmatch(full_name) is not None,
             "GitHub event repository name is invalid")
    event_head = _object(event_pr.get("head"), "GitHub event candidate head is missing")
    event_ref = event_head.get("ref")
    _require(isinstance(event_ref, str) and event_ref.startswith(QUEUE_BRANCH_PREFIX),
             "Mergify candidate has an unexpected head ref")
    owner, repo = full_name.split("/", 1)

    app = _object(github_get("/apps/mergify"), "GitHub App lookup is invalid")
    _require(app.get("id") == MERGIFY_APP_ID and app.get("slug") == "mergify"
             and _field(app.get("owner"), "login") == MERGIFY_APP_OWNER,
             "Could not resolve the official Mergify GitHub App")
    expected_identity = _identity(_object(github_get("/users/mergify%5Bbot%5D"), "GitHub bot lookup is invalid"))
    _require(expected_identity == (MERGIFY_BOT_ID, MERGIFY_BOT_LOGIN, "Bot", MERGIFY_APP_URL),
             "Could not resolve the Mergify GitHub App identity")
    _require(_identity(event_pr.get("user")) == expected_identity,
             "Candidate author is not the verified Mergify GitHub App")
    _require(_identity(event.get("sender")) == expected_identity,
             "Candidate head event was not sent by the verified Mergify GitHub App")

    candidate_number = event_pr.get("number")
    candidate_sha = event_head.get("sha")
    _require(isinstance(candidate_number, int) and candidate_number > 0, "Candidate PR number is invalid")
    _require(is_sha(candidate_sha), "Candidate head SHA is invalid")
    candidate_path = f"/repos/{full_name}/pulls/{candidate_number}"
    def recheck_candidate(message: str = "GitHub candidate lookup returned an invalid payload") -> None:
        candidate = _object(github_get(candidate_path), message)
        _candidate_snapshot(candidate, candidate_number, event_head, repository, expected_identity, base_branch)

    recheck_candidate()

    def live_lineage() -> tuple[str, list[int], list[str]]:
        delay = 2
        while True:
            try:
                return _candidate_lineage(mergify_status(owner, repo, base_branch), candidate_number)
            except CandidateBatchError:
                recheck_candidate("Invalid candidate lookup")
                remaining = batch_deadline - _retry_clock()
                if remaining <= 0:
                    raise
                started = _retry_clock()
                _retry_sleep(min(delay, remaining))
                batch_wait[0] += _retry_clock() - started
                recheck_candidate("Invalid candidate lookup")
                if _retry_clock() >= batch_deadline:
                    raise
                delay = min(delay * 2, 30)

    first_lineage = live_lineage()
    base_path = f"/repos/{full_name}/git/ref/heads/{quote(base_branch)}"
    base_before = github_get(base_path) if first_lineage[2] else None
    source_snapshots: dict[int, tuple[str, tuple[object, ...]]] = {}
    for number in first_lineage[1]:
        source = _lookup_source(github_get, full_name, number)
        source_sha = _attest_source(
            source, number=number, repository=repository, candidate_sha=candidate_sha,
            github_get=github_get, base_branch=base_branch,
        )
        source_snapshots[number] = (source_sha, _source_fingerprint(source))
        try:
            source_policy(number, source)
        except CandidateError as exc:
            current = None
            with contextlib.suppress(Exception):
                current = _lookup_source(github_get, full_name, number)
            if _field(current, "merged") is True and _field(_field(current, "head"), "sha") == source_sha:
                raise CandidateRaceError(f"source PR #{number} landed during attestation") from exc
            raise

    if first_lineage[2]:
        try:
            base_ref = _object(base_before, f"{base_branch} ref unavailable")
            base_sha = _object(base_ref.get("object"), f"{base_branch} object unavailable").get("sha")
            _require(is_sha(base_sha), f"invalid {base_branch} head")
            _verify_tree(full_name, base_sha, candidate_sha,
                         [snapshot[0] for snapshot in source_snapshots.values()])
            _require(github_get(base_path) == base_before,
                     f"{base_branch} changed during attestation", CandidateRaceError)
        except CandidateRaceError:
            raise
        except (CandidateError, OSError, ValueError) as exc:
            _require(github_get(base_path) == base_before, f"{base_branch} changed during attestation",
                     CandidateRaceError)  # A mismatch while main is landing is a race, not a verdict.
            raise CandidateError(
                f"Missing Mergify parent batches {', '.join(first_lineage[2])}: {exc}"
            ) from None

    second_lineage = live_lineage()
    _require(second_lineage == first_lineage,
             "Mergify candidate membership changed during attestation", CandidateRaceError)
    recheck_candidate()
    for number, (expected_sha, expected_fingerprint) in source_snapshots.items():
        current = _lookup_source(github_get, full_name, number)
        _require(_field(current.get("head"), "sha") == expected_sha,
                 f"source PR #{number} head changed during attestation")
        _require(_source_fingerprint(current) == expected_fingerprint,
                 f"source PR #{number} metadata changed during attestation", CandidateRaceError)
    return [{"number": number, "head_sha": sha} for number, (sha, _) in source_snapshots.items()]
