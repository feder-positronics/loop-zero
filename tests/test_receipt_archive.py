from pathlib import Path

import pytest

from loopzero import receipt_archive


def test_archive_copies_all_files_and_accepts_identical(tmp_path, monkeypatch):
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
    assert receipt_archive.archive(files, 'acme/widgets', 7, 'a' * 40) == dest
    (files[0]).write_text('different')
    with pytest.raises(OSError):
        receipt_archive.archive(files, 'acme/widgets', 7, 'a' * 40)
    assert (dest / files[0].name).read_text() != 'different'


def test_digest_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    source = tmp_path / 'review-x.json'
    source.write_text('evidence')
    original = receipt_archive.digest
    monkeypatch.setattr(receipt_archive, 'digest',
                        lambda path: original(path) if path == source else 'bad')
    with pytest.raises(OSError):
        receipt_archive.archive([source], 'acme/widgets', 7, 'a' * 40)
