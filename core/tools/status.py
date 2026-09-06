#!/usr/bin/env python3
"""Compare a consumer's vendored files with its declared immutable Git pin."""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tomllib


def git(source: Path, *args: str) -> bytes:
    return subprocess.run(
        ['git', '--no-replace-objects', '-C', str(source), *args], check=True, capture_output=True
    ).stdout


def verify(consumer: Path, source: Path) -> str:
    consumer = consumer.resolve(strict=True)
    config = tomllib.loads((consumer / 'workflow.toml').read_text())['core']
    revision = config['revision']
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('core.revision must be a full lowercase Git commit SHA')
    if git(source, 'cat-file', '-t', revision).strip() != b'commit':
        raise ValueError('core.revision must identify a commit')
    relative = Path(config['path'])
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('core.path must be a nonempty repository-relative directory')
    vendor = consumer
    for part in relative.parts:
        vendor = vendor / part
        if vendor.is_symlink():
            raise ValueError('core.path must not traverse symlinks')
    if not vendor.is_dir():
        raise ValueError('vendored core directory is missing')

    expected = {}
    tree = git(source, 'ls-tree', '-rz', revision, '--', 'core/')
    for entry in tree.split(b'\0'):
        if not entry:
            continue
        metadata, encoded_path = entry.split(b'\t', 1)
        mode, kind, blob = metadata.split()
        if mode not in (b'100644', b'100755') or kind != b'blob':
            raise ValueError('source core must contain ordinary files only')
        name = encoded_path.decode('utf-8').removeprefix('core/')
        expected[name] = git(source, 'cat-file', 'blob', blob.decode('ascii'))
    if not expected:
        raise ValueError('pin has no core files')

    actual = {}
    for path in vendor.rglob('*'):
        if path.is_symlink():
            raise ValueError('vendored core must not contain symlinks')
        if path.is_file():
            actual[path.relative_to(vendor).as_posix()] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError('vendored core must contain ordinary files only')
    added = sorted(actual.keys() - expected.keys())
    missing = sorted(expected.keys() - actual.keys())
    changed = sorted(k for k in actual.keys() & expected.keys() if actual[k] != expected[k])
    if added or missing or changed:
        raise ValueError(f'core differs from pin: added={added}, missing={missing}, changed={changed}')
    return revision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--consumer', type=Path, default=Path.cwd())
    parser.add_argument('--source', type=Path, required=True,
                        help='local loop-zero Git checkout containing the pinned commit')
    args = parser.parse_args()
    try:
        revision = verify(args.consumer, args.source)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f'UNVERIFIED: {exc}', file=sys.stderr)
        return 1
    print(f'VERIFIED: vendored core bytes match {revision}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
