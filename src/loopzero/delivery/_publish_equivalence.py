"""Fail-closed, complete author-tree replay for leased republication."""

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ..kernel.patch_identity import (
    PATCH_DIFF_FLAGS,
    PatchIdentityError,
    _git,
    _reject_repository_attributes,
)

INDEX = "docs/design/explorations/index.md"
START = "<!-- .generated-index-start -->"
END = "<!-- .generated-index-end -->"
OID = re.compile(r"[0-9a-f]{40}")


def _checked(repo: Path, *args: str, **kwargs) -> bytes:
    result = _git(repo, *args, timeout_s=30, **kwargs)
    if result.returncode:
        raise PatchIdentityError("republication proof command failed")
    return result.stdout


def _oid(repo: Path, *args: str, **kwargs) -> str:
    value = _checked(repo, *args, **kwargs).decode("ascii").strip()
    if OID.fullmatch(value) is None:
        raise PatchIdentityError("ambiguous republication object identity")
    return value


def _section(text: str) -> tuple[str, str]:
    if text.count(START) != 1 or text.count(END) != 1:
        raise PatchIdentityError("ambiguous generated index markers")
    prefix, content = text.split(START)
    _, suffix = content.split(END)
    return prefix, suffix


def _normalized_tree(repo: Path, commit: str, index: Path) -> str:
    env = {"GIT_INDEX_FILE": str(index)}
    _checked(repo, "read-tree", commit, env=env)
    mode = _checked(repo, "ls-tree", commit, "--", INDEX).split()
    if not mode or mode[0] != b"100644":
        raise PatchIdentityError("generated index must be a regular blob")
    text = _checked(repo, "show", f"{commit}:{INDEX}").decode("utf-8")
    prefix, suffix = _section(text)
    blob = _oid(
        repo,
        "hash-object",
        "-w",
        "--stdin",
        input_bytes=(prefix + START + END + suffix).encode(),
    )
    _checked(repo, "update-index", "--cacheinfo", f"100644,{blob},{INDEX}", env=env)
    return _oid(repo, "write-tree", env=env)


def _regenerate(repo: Path, commit: str, scratch: Path) -> dict[str, str]:
    """Run only the publisher's trusted generator on extracted regular data."""
    generator_path = Path(__file__).resolve().parent / "_docs" / "generate_indexes.py"
    sys.path.insert(0, str(generator_path.parent))
    try:
        spec = importlib.util.spec_from_file_location(
            "republish_index_generator", generator_path
        )
        if spec is None or spec.loader is None:
            raise PatchIdentityError("trusted index generator unavailable")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        directory = scratch / "docs/design/explorations"
        directory.mkdir(parents=True)
        entries = _checked(
            repo, "ls-tree", "-z", commit, "--", "docs/design/explorations/"
        )
        # ls-tree without -r lists only immediate children of the named directory.
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, blob = metadata.split()
            path = raw_path.decode("utf-8")
            if Path(path).parent.as_posix() != "docs/design/explorations":
                raise PatchIdentityError("unexpected generator input path")
            if Path(path).name == ".no-generate-index":
                raise PatchIdentityError("generator opted out")
            if path.endswith(".md"):
                if mode != b"100644" or kind != b"blob":
                    raise PatchIdentityError("nonregular generator input")
                (scratch / path).write_bytes(
                    _checked(repo, "cat-file", "blob", blob.decode())
                )
        target = scratch / INDEX
        original = target.read_bytes().decode("utf-8")
        _section(original)
        generator.ROOT = scratch
        entries = generator.collect_entries(directory, "index.md", "direct")
        generated = generator.pick_template("docs/design/explorations")(entries)
        regenerated = generator.inject_managed_section(original, generated)
        if regenerated != original:
            raise PatchIdentityError("generated index does not match regeneration")
        return {
            "commit": commit,
            "path": INDEX,
            "generated_on": datetime.now(UTC).date().isoformat(),
            "blob_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "generator_sha256": hashlib.sha256(generator_path.read_bytes()).hexdigest(),
            "policy_sha256": hashlib.sha256(
                json.dumps(generator.POLICY, sort_keys=True).encode()
            ).hexdigest(),
        }
    finally:
        sys.path.pop(0)


def _replay(
    repo: Path, old_base: str, old: str, new_base: str, new: str, index: Path
) -> tuple[str, str] | None:
    patch = _checked(repo, "diff", *PATCH_DIFF_FLAGS, old_base, old)
    if not patch:
        return None
    env = {"GIT_INDEX_FILE": str(index)}
    _checked(repo, "read-tree", new_base, env=env)
    result = _git(
        repo,
        "apply",
        "--cached",
        "--3way",
        "--binary",
        "--whitespace=nowarn",
        "-",
        input_bytes=patch,
        env=env,
        timeout_s=30,
    )
    if result.returncode or _checked(repo, "ls-files", "-u", env=env):
        return None
    tree = _oid(repo, "write-tree", env=env)
    if tree != _oid(repo, "rev-parse", f"{new}^{{tree}}"):
        return None
    return tree, hashlib.sha256(patch).hexdigest()


def prove_republish_equivalence(
    *, repo: Path, observed_head: str, expected_head: str, base_head: str
) -> dict[str, object] | None:
    """No remote mutation, candidate execution, working-tree or real-index edits."""
    try:
        if any(
            OID.fullmatch(sha) is None
            for sha in (observed_head, expected_head, base_head)
        ):
            return None
        _reject_repository_attributes(repo)
        old_tree = _oid(repo, "rev-parse", f"{observed_head}^{{tree}}")
        new_tree = _oid(repo, "rev-parse", f"{expected_head}^{{tree}}")
        proof: dict[str, object] = {
            "schema_version": "republish-equivalence-v1",
            "observed_head": observed_head,
            "expected_head": expected_head,
            "base_head": base_head,
            "result_tree_sha": new_tree,
        }
        if old_tree == new_tree:
            proof["method"] = "identical-tree"
        else:
            old_base = _oid(repo, "merge-base", "--all", observed_head, base_head)
            new_base = _oid(repo, "merge-base", "--all", expected_head, base_head)
            if _git(
                repo, "merge-base", "--is-ancestor", old_base, new_base, timeout_s=30
            ).returncode:
                return None
            proof.update(old_base=old_base, new_base=new_base)
            with tempfile.TemporaryDirectory(
                prefix="republish-equivalence-"
            ) as temporary:
                scratch = Path(temporary)
                replay = _replay(
                    repo,
                    old_base,
                    observed_head,
                    new_base,
                    expected_head,
                    scratch / "index",
                )
                if replay:
                    proof["method"] = "complete-author-replay"
                else:
                    generations = [
                        _regenerate(repo, commit, scratch / str(i))
                        for i, commit in enumerate((observed_head, expected_head))
                    ]
                    trees = [
                        _normalized_tree(repo, commit, scratch / f"index-{i}")
                        for i, commit in enumerate(
                            (old_base, observed_head, new_base, expected_head)
                        )
                    ]
                    replay = _replay(repo, *trees, scratch / "normalized-index")
                    if replay is None:
                        return None
                    proof.update(
                        method="regenerated-author-replay",
                        regeneration=generations,
                        normalized_result_tree_sha=replay[0],
                    )
                proof["author_patch_sha256"] = replay[1]
        proof["receipt_digest"] = hashlib.sha256(
            json.dumps(proof, sort_keys=True).encode()
        ).hexdigest()
        return proof
    except (
        AttributeError,
        OSError,
        ValueError,
        UnicodeError,
        ImportError,
        PatchIdentityError,
        subprocess.TimeoutExpired,
    ):
        return None
