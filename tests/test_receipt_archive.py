from pathlib import Path

import pytest

from loopzero import receipt_archive


@pytest.mark.parametrize('redirect', [None, 'root', 'dest'])
def test_archive_copies_all_files_and_rejects_symlinks(tmp_path, monkeypatch, redirect):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = tmp_path / 'clone' / '.loopzero'
    source.mkdir(parents=True)
    for name in ('review-head-primary.json', 'review-head-primary-pair-attempt.json',
                 'review-head-primary-pair-1-native.json', 'review-head-primary-pair-1-last.json'):
        (source / name).write_bytes(name.encode())
    files = list(source.iterdir())
    dest = receipt_archive.archive(files, 'acme/widgets', 7, 'a' * 40)
    assert {p.name: p.read_bytes() for p in dest.iterdir()} == {
        p.name: p.read_bytes() for p in files}
    if redirect:
        path = dest.parent if redirect == 'root' else dest
        path.rename(tmp_path / 'real')
        path.symlink_to(tmp_path / 'real')
        with pytest.raises(OSError):
            receipt_archive.archive(files, 'acme/widgets', 7, 'a' * 40)
