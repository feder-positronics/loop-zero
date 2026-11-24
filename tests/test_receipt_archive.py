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


def test_interrupted_staging_can_be_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    files = [tmp_path / name for name in ("first.json", "second.json")]
    for path in files:
        path.write_text("{}")
    root = tmp_path / ".local/state/loopzero/receipts/acme-widgets"
    dest = root / ("7-" + "a" * 40)

    def interrupt(source, target):
        target.write(source.read(1))
        assert not dest.exists()
        raise OSError("staging interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(receipt_archive.shutil, "copyfileobj", interrupt)
        with pytest.raises(OSError, match="staging interrupted"):
            receipt_archive.archive(files, "acme/widgets", 7, "a" * 40)
    assert list(root.iterdir()) == []

    assert receipt_archive.archive(files, "acme/widgets", 7, "a" * 40) == dest
    assert receipt_archive.archive(files, "acme/widgets", 7, "a" * 40) == dest
    assert list(root.iterdir()) == [dest]
    assert dest.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in dest.iterdir())


@pytest.mark.parametrize("identical", [True, False])
def test_archive_verifies_concurrent_publication(tmp_path, monkeypatch, identical):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = tmp_path / "receipt.json"
    source.write_text("{}")
    rename = receipt_archive.os.rename

    def publish_concurrently(stage, dest):
        assert not dest.exists()
        dest.mkdir(mode=0o700)
        (dest / source.name).write_text("{}" if identical else "[]")
        rename(stage, dest)

    monkeypatch.setattr(receipt_archive.os, "rename", publish_concurrently)
    root = tmp_path / ".local/state/loopzero/receipts/acme-widgets"
    dest = root / ("7-" + "a" * 40)
    if identical:
        assert receipt_archive.archive([source], "acme/widgets", 7, "a" * 40) == dest
    else:
        with pytest.raises(OSError, match="digest mismatch"):
            receipt_archive.archive([source], "acme/widgets", 7, "a" * 40)
    assert list(root.iterdir()) == [dest]
    assert (dest / source.name).read_text() == ("{}" if identical else "[]")
