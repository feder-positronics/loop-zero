"""Read selected regular Git blobs without consulting candidate configuration."""

import fnmatch
import hashlib
import json
import os
import re
import subprocess
from pathlib import PurePosixPath


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def environment():
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def git(root, *args, input=None):
    result = subprocess.run(
        ["/usr/bin/git", "-C", os.fspath(root), *args],
        env=environment(),
        capture_output=True,
        timeout=60,
        check=False,
        input=input,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def within(path, roots):
    return any(
        root == "." or path == root or path.startswith(root + "/") for root in roots
    )


def physical_lines(text):
    """Universal source newlines; Unicode separators/form feeds are not lines."""
    if not text:
        return []
    lines = re.split(r"\r\n|\r|\n", text)
    return lines[:-1] if text.endswith(("\r", "\n")) else lines


def read_source(root, ref, policy):
    sha = (
        git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
        .decode()
        .strip()
    )
    files, excluded, errors = {}, [], []
    entries = git(root, "ls-tree", "-rz", "--full-tree", sha).split(b"\0")
    pending = []
    for entry in filter(None, entries):
        metadata, raw_path = entry.split(b"\t", 1)
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if not within(path, policy["scope"]):
            continue
        mode, kind, oid = metadata.decode().split()
        reason = None
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in policy["exclude"]):
            reason = "policy exclusion"
        elif mode not in ("100644", "100755") or kind != "blob":
            reason = "non-regular source"
        elif PurePosixPath(path).suffix not in (".py", ".ts", ".tsx"):
            reason = "unsupported format"
        if reason:
            excluded.append({"path": path, "reason": reason})
            continue
        if ".." in PurePosixPath(path).parts or path.startswith("/"):
            errors.append(f"unsafe source path: {path}")
            continue
        pending.append((path, oid))
    query = "".join(oid + "\n" for _, oid in pending).encode()
    sizes = (
        git(root, "cat-file", "--batch-check=%(objectsize)", input=query).splitlines()
        if pending
        else []
    )
    accepted, total = [], 0
    for (path, oid), raw_size in zip(pending, sizes, strict=True):
        size = int(raw_size)
        if size > 5_000_000 or total + size > 100_000_000:
            errors.append(f"source size limit: {path}")
            continue
        total += size
        accepted.append((path, oid, size))
    query = "".join(oid + "\n" for _, oid, _ in accepted).encode()
    blobs = git(root, "cat-file", "--batch", input=query) if accepted else b""
    offset = 0
    for path, oid, size in accepted:
        header_end = blobs.index(b"\n", offset)
        header = blobs[offset:header_end].decode()
        if header != f"{oid} blob {size}":
            raise ValueError("Git batch response does not match requested object")
        start = header_end + 1
        content_bytes = blobs[start : start + size]
        offset = start + size + 1
        try:
            content = content_bytes.decode("utf-8-sig")
        except UnicodeError:
            errors.append(f"non-UTF-8 source: {path}")
            continue
        files[path] = {
            "text": content,
            "blob": oid,
            "lines": len(physical_lines(content)),
            "category": "tests"
            if within(path, policy["test_root"])
            or any(
                fnmatch.fnmatchcase(path, pattern)
                for pattern in policy.get("test_pattern", [])
            )
            else "production",
        }
    if not files:
        errors.append("scope contains no supported source files")
    return sha, files, excluded, errors
