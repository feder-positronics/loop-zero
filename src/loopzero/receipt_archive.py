"""Private, append-only handoff of native review evidence."""

import hashlib
import re
import shutil
from pathlib import Path


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def archive(files: list[Path], repo: str, pr: int, head: str) -> Path:
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or not re.fullmatch(r'[0-9a-f]{40,64}', head):
        raise OSError('invalid receipt archive identity')
    root = Path.home() / '.local/state/loopzero/receipts' / repo.replace('/', '-')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = root / f'{pr}-{head}'
    try:
        dest.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        created = False
    if dest.is_symlink() or not dest.is_dir():
        raise OSError('unsafe receipt archive')
    if not created and {p.name for p in dest.iterdir()} != {p.name for p in files}:
        raise OSError('receipt archive file set differs')
    for source in files:
        target = dest / source.name
        if source.is_symlink() or not source.is_file():
            raise OSError('unsafe receipt source')
        before = digest(source)
        if created:
            with target.open('xb') as output:
                target.chmod(0o600)
                with source.open('rb') as stream:
                    shutil.copyfileobj(stream, output)
        if target.is_symlink() or not target.is_file() or digest(target) != before or digest(source) != before:
            raise OSError('receipt archive digest mismatch')
    return dest
