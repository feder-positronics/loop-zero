"""Private, append-only handoff of native review evidence."""

import errno
import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _verify(files: list[Path], dest: Path) -> None:
    if not stat.S_ISDIR(dest.lstat().st_mode):
        raise OSError('unsafe receipt archive')
    if {p.name for p in dest.iterdir()} != {p.name for p in files}:
        raise OSError('receipt archive file set differs')
    for source in files:
        if source.is_symlink() or not source.is_file():
            raise OSError('unsafe receipt source')
        target = dest / source.name
        before = digest(source)
        if target.is_symlink() or not target.is_file() or digest(target) != before or digest(source) != before:
            raise OSError('receipt archive digest mismatch')


def archive(files: list[Path], repo: str, pr: int, head: str) -> Path:
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise OSError('invalid receipt archive identity')
    root = Path.home() / '.local/state/loopzero/receipts' / repo.replace('/', '-')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise OSError('unsafe receipt archive root')
    dest = root / f'{pr}-{head}'
    if dest.exists() or dest.is_symlink():
        _verify(files, dest)
        return dest

    with tempfile.TemporaryDirectory(dir=root, prefix=f'.{dest.name}-') as staging:
        stage = Path(staging)
        for source in files:
            if source.is_symlink() or not source.is_file():
                raise OSError('unsafe receipt source')
            before = digest(source)
            target = stage / source.name
            with target.open('xb') as output:
                target.chmod(0o600)
                with source.open('rb') as stream:
                    shutil.copyfileobj(stream, output)
            if digest(target) != before or digest(source) != before:
                raise OSError('receipt archive digest mismatch')
        try:
            os.rename(stage, dest)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
        else:
            return dest
    _verify(files, dest)
    return dest
