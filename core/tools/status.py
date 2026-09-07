#!/usr/bin/env python3
"""Read-only pin, child-environment and known-gaps checks; verdicts are pass/fail."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib


# No inherited Git configuration, repository routing, or commit authority.
GIT_ENV_KEYS = ('PATH', 'SYSTEMROOT', 'WINDIR', 'TMPDIR', 'TEMP', 'TMP')


def check_child_env(environment) -> None:
    if any(re.search(r'(^|[^A-Z0-9])(LEASE|NONCE)(?=$|[^A-Z0-9])', key.upper())
           for key in environment):
        raise ValueError('child environment contains lease/nonce authority')


def git_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in GIT_ENV_KEYS if key in os.environ}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0')
    check_child_env(environment)
    return environment


def check_known_gaps(path: Path) -> None:
    lines = path.read_text().splitlines()
    if not lines or lines[0] != '# Known gaps':
        raise ValueError('known gaps must start with # Known gaps')
    entries = [line for line in lines[1:] if line.strip()]
    if any(not line.startswith('- ') or not line[2:].strip() for line in entries):
        raise ValueError('known gaps require one nonempty - entry per line')
    if len(entries) > 30:
        raise ValueError('known gaps exceed the cap of 30')


def git(source: Path, *args: str) -> bytes:
    return subprocess.run(
        ['git', '--no-replace-objects', '-C', str(source), *args],
        check=True, capture_output=True, env=git_environment()
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
    parser.add_argument('--source', type=Path,
                        help='local loop-zero Git checkout containing the pinned commit')
    parser.add_argument('--known-gaps', type=Path, help='check the repository-owned debt list')
    parser.add_argument('--check-child-env', action='store_true',
                        help='reject lease/nonce names in this process environment')
    args = parser.parse_args()
    if not (args.source or args.known_gaps or args.check_child_env):
        parser.error('select --source, --known-gaps or --check-child-env')
    try:
        if args.check_child_env:
            check_child_env(os.environ)
        if args.known_gaps:
            check_known_gaps(args.known_gaps)
        if args.source:
            verify(args.consumer, args.source)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print('fail')
        print(f'UNVERIFIED: {exc}', file=sys.stderr)
        return 1
    print('pass')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
