#!/usr/bin/env python3
"""Deterministic author-patch identity and replay-equivalence receipts."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

PATCH_IDENTITY_SCHEMA = "patch-identity-v1"
PATCH_EQUIVALENCE_SCHEMA = "patch-equivalence-v1"
PATCH_DIFF_FORMAT = "git-binary-full-index-no-renames-v1"
PATCH_DIFF_FLAGS = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--binary",
    "--full-index",
    "--no-renames",
    "--default-prefix",
    "--diff-algorithm=myers",
    "--no-indent-heuristic",
    "--unified=3",
)
RANGE_DIFF_FLAGS = (
    "--no-dual-color",
    "--creation-factor=100",
    "-U0",
    "--no-renames",
    "--no-ext-diff",
    "--no-textconv",
    "--diff-algorithm=myers",
    "--no-indent-heuristic",
)
_OID_RE = re.compile(r"[0-9a-f]{40,64}")
_PAIRING_RE = re.compile(r"^\d+:\s+[0-9a-f]+\s+[=!]\s+\d+:\s+[0-9a-f]+(?:\s|$)")
_UNPAIRED_RE = re.compile(
    r"^(?:\d+:\s+[0-9a-f]+\s+<\s+-:\s+-{7,}(?:\s|$)|" r"-:\s+-{7,}\s+>\s+\d+:)"
)
_FILE_SECTION_RE = re.compile(
    r"^(?:[^@\s]+|.+ \((?:new|deleted)\)|.+:\d+(?:,\d+)? .+:\d+(?:,\d+)? @@|.+: .+)$"
)


class PatchIdentityError(RuntimeError):
    """Patch identity cannot be established without ambiguity."""


def _git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    hostile_names = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIFF_OPTS",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
    for name in tuple(environment):
        if name in hostile_names or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            environment.pop(name)
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C.UTF-8",
        }
    )
    if extra:
        environment.update(extra)
    return environment


def _git(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo.resolve()), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
        env=_git_environment(env),
    )


def _require_stdout(repo: Path, *args: str) -> str:
    completed = _git(repo, *args)
    value = completed.stdout.decode("ascii", errors="strict").strip()
    if completed.returncode != 0 or _OID_RE.fullmatch(value) is None:
        raise PatchIdentityError(f"git {args[0]} could not resolve one object")
    return value


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _reject_repository_attributes(repo: Path) -> None:
    resolved = _git(
        repo,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "info/attributes",
    )
    if resolved.returncode != 0:
        raise PatchIdentityError("repository attributes path is ambiguous")
    try:
        path = Path(resolved.stdout.decode("utf-8", errors="strict").strip())
        if path.is_symlink() or path.exists():
            raise PatchIdentityError(
                "repository-local attributes make fixed patch bytes ambiguous"
            )
    except OSError as exc:
        raise PatchIdentityError("repository attributes cannot be inspected") from exc


def fixed_patch_bytes(repo: Path, base_sha: str, candidate_sha: str) -> bytes:
    """Return the raw, fixed-flag Git patch used by PatchIdentityV1."""
    _reject_repository_attributes(repo)
    completed = _git(repo, "diff", *PATCH_DIFF_FLAGS, base_sha, candidate_sha)
    if completed.returncode != 0 or not completed.stdout:
        raise PatchIdentityError("fixed patch is missing, empty, or unreadable")
    return completed.stdout


def compute_patch_identity(
    repo: Path, *, base_sha: str, candidate_sha: str
) -> dict[str, object]:
    """Compute PatchIdentityV1 from two immutable Git commits."""
    base_commit = _require_stdout(repo, "rev-parse", f"{base_sha}^{{commit}}")
    candidate_commit = _require_stdout(repo, "rev-parse", f"{candidate_sha}^{{commit}}")
    base_tree = _require_stdout(repo, "rev-parse", f"{base_commit}^{{tree}}")
    candidate_tree = _require_stdout(repo, "rev-parse", f"{candidate_commit}^{{tree}}")
    patch = fixed_patch_bytes(repo, base_commit, candidate_commit)
    patch_id_result = _git(repo, "patch-id", "--verbatim", input_bytes=patch)
    fields = patch_id_result.stdout.decode("ascii", errors="replace").split()
    if (
        patch_id_result.returncode != 0
        or len(fields) != 2
        or _OID_RE.fullmatch(fields[0]) is None
        or _OID_RE.fullmatch(fields[1]) is None
    ):
        raise PatchIdentityError("git patch-id --verbatim returned malformed output")
    return {
        "schema_version": PATCH_IDENTITY_SCHEMA,
        "base_sha": base_commit,
        "base_tree_sha": base_tree,
        "candidate_sha": candidate_commit,
        "candidate_tree_sha": candidate_tree,
        "diff_format": PATCH_DIFF_FORMAT,
        "diff_sha256": hashlib.sha256(patch).hexdigest(),
        "patch_id_verbatim": fields[0],
    }


def capture_patch_identity(
    repo: Path, *, candidate_sha: str, base_ref: str = "origin/main"
) -> dict[str, object] | None:
    """Capture the candidate's trusted merge base, or fail closed to no identity."""
    try:
        candidate = _require_stdout(repo, "rev-parse", f"{candidate_sha}^{{commit}}")
        base_tip = _require_stdout(repo, "rev-parse", f"{base_ref}^{{commit}}")
        merged = _git(repo, "merge-base", candidate, base_tip)
        base = merged.stdout.decode("ascii", errors="replace").strip()
        if (
            merged.returncode != 0
            or _OID_RE.fullmatch(base) is None
            or _git(repo, "merge-base", "--is-ancestor", base, candidate).returncode
            != 0
        ):
            return None
        return compute_patch_identity(repo, base_sha=base, candidate_sha=candidate)
    except (PatchIdentityError, UnicodeError):
        return None


def _validate_identity(repo: Path, identity: Mapping[str, object]) -> bytes:
    expected_keys = {
        "schema_version",
        "base_sha",
        "base_tree_sha",
        "candidate_sha",
        "candidate_tree_sha",
        "diff_format",
        "diff_sha256",
        "patch_id_verbatim",
    }
    if set(identity) != expected_keys:
        raise PatchIdentityError("patch identity fields are missing or ambiguous")
    if (
        identity.get("schema_version") != PATCH_IDENTITY_SCHEMA
        or identity.get("diff_format") != PATCH_DIFF_FORMAT
    ):
        raise PatchIdentityError("patch identity schema or diff format is unsupported")
    for field in (
        "base_sha",
        "base_tree_sha",
        "candidate_sha",
        "candidate_tree_sha",
        "patch_id_verbatim",
    ):
        value = identity.get(field)
        if not isinstance(value, str) or _OID_RE.fullmatch(value) is None:
            raise PatchIdentityError(f"patch identity {field} is invalid")
    diff_digest = identity.get("diff_sha256")
    if (
        not isinstance(diff_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", diff_digest) is None
    ):
        raise PatchIdentityError("patch identity diff digest is invalid")
    recomputed = compute_patch_identity(
        repo,
        base_sha=str(identity["base_sha"]),
        candidate_sha=str(identity["candidate_sha"]),
    )
    ancestry = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        str(identity["base_sha"]),
        str(identity["candidate_sha"]),
    )
    if ancestry.returncode != 0 or recomputed != dict(identity):
        raise PatchIdentityError("stored patch identity does not match Git objects")
    return fixed_patch_bytes(
        repo, str(identity["base_sha"]), str(identity["candidate_sha"])
    )


def _changed_paths(repo: Path, left: str, right: str) -> set[str]:
    completed = _git(repo, "diff", "--name-only", "--no-renames", left, right)
    if completed.returncode != 0:
        raise PatchIdentityError("changed-path comparison failed")
    return {
        line
        for line in completed.stdout.decode(
            "utf-8", errors="surrogateescape"
        ).splitlines()
        if line
    }


def prove_patch_equivalence(
    repo: Path,
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> dict[str, object] | None:
    """Replay the exact left patch on the right base and require the right tree."""
    try:
        left_patch = _validate_identity(repo, left)
        _validate_identity(repo, right)
        if (
            _git(
                repo,
                "merge-base",
                "--is-ancestor",
                str(left["base_sha"]),
                str(right["base_sha"]),
            ).returncode
            != 0
        ):
            return None
        if left["patch_id_verbatim"] != right["patch_id_verbatim"]:
            return None
        with tempfile.TemporaryDirectory(prefix="patch-equivalence-") as temporary:
            index = Path(temporary) / "index"
            index_environment = {"GIT_INDEX_FILE": str(index)}
            read_tree = _git(
                repo,
                "read-tree",
                str(right["base_tree_sha"]),
                env=index_environment,
            )
            if read_tree.returncode != 0:
                return None
            applied = _git(
                repo,
                "apply",
                "--cached",
                "--3way",
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_bytes=left_patch,
                env=index_environment,
            )
            unmerged = _git(repo, "ls-files", "-u", env=index_environment)
            if applied.returncode != 0 or unmerged.returncode != 0 or unmerged.stdout:
                return None
            written = _git(repo, "write-tree", env=index_environment)
            result_tree = written.stdout.decode("ascii", errors="replace").strip()
            if written.returncode != 0 or result_tree != right["candidate_tree_sha"]:
                return None
        author_paths = _changed_paths(
            repo, str(left["base_sha"]), str(left["candidate_sha"])
        )
        base_motion_paths = _changed_paths(
            repo, str(left["base_sha"]), str(right["base_sha"])
        )
        version = _git(repo, "--version")
        if version.returncode != 0:
            return None
        payload: dict[str, object] = {
            "schema_version": PATCH_EQUIVALENCE_SCHEMA,
            "left_identity_digest": _canonical_digest(dict(left)),
            "right_identity_digest": _canonical_digest(dict(right)),
            "left_diff_sha256": left["diff_sha256"],
            "right_diff_sha256": right["diff_sha256"],
            "replay_command": [
                "git",
                "apply",
                "--cached",
                "--3way",
                "--binary",
                "--whitespace=nowarn",
                "-",
            ],
            "git_version": version.stdout.decode("ascii", errors="replace").strip(),
            "return_code": 0,
            "result_tree_sha": result_tree,
            "base_path_overlap": sorted(author_paths & base_motion_paths),
        }
        payload["receipt_digest"] = _canonical_digest(payload)
        return payload
    except (OSError, PatchIdentityError, UnicodeError):
        return None


def prove_commit_carry(
    repo: Path,
    reviewed_identity: Mapping[str, object],
    *,
    current_head: str,
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Prove one immutable commit carries the persisted reviewed patch."""
    current_identity = capture_patch_identity(repo, candidate_sha=current_head)
    if current_identity is None:
        return None
    receipt = prove_patch_equivalence(repo, reviewed_identity, current_identity)
    return (current_identity, receipt) if receipt is not None else None


def prove_clean_head_carry(
    repo: Path,
    reviewed_identity: Mapping[str, object],
    *,
    current_head: str,
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Prove one clean current head carries the persisted reviewed patch."""
    status = _git(repo, "status", "--porcelain")
    if status.returncode != 0 or status.stdout:
        return None
    return prove_commit_carry(repo, reviewed_identity, current_head=current_head)


def build_patch_carry_record(
    terminal: Mapping[str, object],
    *,
    current_source: Mapping[str, object],
    current_identity: Mapping[str, object],
    replay_receipt: Mapping[str, object],
) -> dict[str, object]:
    """Build the append-only note for carried model-review evidence."""
    return {
        "type": "attempt-patch-identity-carry",
        "task_id": terminal.get("task_id"),
        "work_unit_id": terminal.get("work_unit_id"),
        "run_id": terminal.get("run_id"),
        "review_lens": terminal.get("review_lens"),
        "snapshot_sha": terminal.get("snapshot_sha"),
        "snapshot_tree_sha": terminal.get("snapshot_tree_sha"),
        "patch_identity": terminal.get("patch_identity"),
        "carried_to_source_identity": dict(current_source),
        "carried_to_patch_identity": dict(current_identity),
        "patch_equivalence": dict(replay_receipt),
        "authority_scope": "model-review-only",
        "invalidated_receipts": ["validation", "final-ci"],
        "status": "carried",
    }


def equivalence_receipt_is_valid(receipt: object) -> bool:
    """Validate the closed PatchEquivalenceV1 receipt shape and digest."""
    if not isinstance(receipt, Mapping):
        return False
    expected = {
        "schema_version",
        "left_identity_digest",
        "right_identity_digest",
        "left_diff_sha256",
        "right_diff_sha256",
        "replay_command",
        "git_version",
        "return_code",
        "result_tree_sha",
        "base_path_overlap",
        "receipt_digest",
    }
    if (
        set(receipt) != expected
        or receipt.get("schema_version") != PATCH_EQUIVALENCE_SCHEMA
    ):
        return False
    for field in (
        "left_identity_digest",
        "right_identity_digest",
        "left_diff_sha256",
        "right_diff_sha256",
        "receipt_digest",
    ):
        if (
            not isinstance(receipt.get(field), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(receipt[field])) is None
        ):
            return False
    if (
        receipt.get("replay_command")
        != [
            "git",
            "apply",
            "--cached",
            "--3way",
            "--binary",
            "--whitespace=nowarn",
            "-",
        ]
        or not str(receipt.get("git_version") or "").startswith("git version ")
        or receipt.get("return_code") != 0
        or not isinstance(receipt.get("result_tree_sha"), str)
        or _OID_RE.fullmatch(str(receipt["result_tree_sha"])) is None
        or not isinstance(receipt.get("base_path_overlap"), list)
        or not all(isinstance(path, str) for path in receipt["base_path_overlap"])
    ):
        return False
    payload = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    return receipt["receipt_digest"] == _canonical_digest(payload)


def validate_patch_carry(
    repo: Path,
    terminal: Mapping[str, object],
    carry: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]] | None:
    """Recompute and bind one durable carry to its accepted review terminal."""
    terminal_identity = terminal.get("patch_identity")
    current_source = carry.get("carried_to_source_identity")
    current_identity = carry.get("carried_to_patch_identity")
    recorded_receipt = carry.get("patch_equivalence")
    if (
        carry.get("type") != "attempt-patch-identity-carry"
        or carry.get("status") != "carried"
        or carry.get("authority_scope") != "model-review-only"
        or carry.get("invalidated_receipts") != ["validation", "final-ci"]
        or any(
            carry.get(field) != terminal.get(field)
            for field in (
                "task_id",
                "work_unit_id",
                "run_id",
                "snapshot_sha",
                "snapshot_tree_sha",
                "patch_identity",
            )
        )
        or not isinstance(terminal_identity, Mapping)
        or not isinstance(current_source, Mapping)
        or not isinstance(current_identity, Mapping)
        or current_source.get("head") != current_identity.get("candidate_sha")
        or not equivalence_receipt_is_valid(recorded_receipt)
    ):
        return None
    recomputed = prove_patch_equivalence(repo, terminal_identity, current_identity)
    if recomputed is None or recomputed != dict(recorded_receipt):
        return None
    return dict(current_source), dict(current_identity), recomputed


def parse_range_diff_churn(output: bytes) -> int | None:
    """Count outer author-patch payload rows, rejecting unrecognized grammar."""
    churn = 0
    paired_commit = False
    section = "none"
    file_payload_seen = False
    lines = output.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.decode("utf-8", errors="replace")
        if not line:
            continue
        leading_spaces = len(line) - len(line.lstrip(" "))
        header = line[leading_spaces:]
        if leading_spaces < 4 and _UNPAIRED_RE.match(header):
            return None
        if leading_spaces < 4 and _PAIRING_RE.match(header):
            paired_commit = True
            section = "none"
            file_payload_seen = False
            continue
        if line[:1] != " ":
            return None
        if not paired_commit:
            return None
        if line.startswith("    @@"):
            heading = line[6:].strip().lower()
            if heading in {"metadata", "commit message"}:
                section = "metadata"
                file_payload_seen = False
            elif _FILE_SECTION_RE.fullmatch(heading):
                section = "file"
                file_payload_seen = False
            else:
                return None
            continue
        if section == "file" and len(raw_line) >= 6 and raw_line[:4] == b"    ":
            outer, inner = raw_line[4:5], raw_line[5:6]
            if outer in {b"+", b"-"}:
                if inner in {b"+", b"-"}:
                    churn += 1
                    file_payload_seen = True
                elif inner not in {b" ", b"@"}:
                    return None
            if any(
                marker in raw_line
                for marker in (
                    b"Binary files ",
                    b"GIT binary patch",
                    b"new file mode ",
                    b"deleted file mode ",
                    b"old mode ",
                    b"new mode ",
                )
            ):
                return None
        elif (
            section == "file"
            and raw_line[:4] == b"    "
            and raw_line[4:5] in {b"+", b"-"}
        ):
            next_line = lines[index + 1] if index + 1 < len(lines) else b""
            if not next_line.startswith(
                (b"    + ## ", b"    - ## ", b"    @@ ")
            ) and not (not next_line and file_payload_seen):
                return None
        elif (
            section != "metadata"
            and len(raw_line) >= 6
            and raw_line[4:5]
            in {
                b"+",
                b"-",
            }
        ):
            return None
    return churn if paired_commit else None


def patch_delta_churn(
    repo: Path,
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> tuple[int, dict[str, object] | None] | None:
    """Return cumulative author-patch churn, or None for mandatory full review."""
    equivalence = prove_patch_equivalence(repo, left, right)
    if equivalence is not None:
        return 0, equivalence
    try:
        _validate_identity(repo, left)
        _validate_identity(repo, right)
    except PatchIdentityError:
        return None
    completed = _git(
        repo,
        "range-diff",
        *RANGE_DIFF_FLAGS,
        f"{left['base_sha']}..{left['candidate_sha']}",
        f"{right['base_sha']}..{right['candidate_sha']}",
    )
    if completed.returncode != 0:
        return None
    churn = parse_range_diff_churn(completed.stdout)
    return (churn, None) if churn is not None else None
