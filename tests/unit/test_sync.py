import fcntl
import multiprocessing
from pathlib import Path

import pytest

from loopzero import config, sync


def _check_in_separate_process(root: str, started, finished) -> None:
    started.set()
    sync.check(config.load_profile(Path(root)))
    finished.set()


def test_check_reports_drift_then_write_then_clean(consumer: Path):
    profile = config.load_profile(consumer)
    drift = sync.check(profile)
    assert Path("AGENTS.md") in drift and Path("CLAUDE.md") in drift
    changed = sync.write(profile)
    assert set(changed) == set(drift)
    assert sync.check(profile) == []
    assert sync.write(profile) == [], "second write is a no-op"


def test_managed_block_preserves_owner_content_and_is_idempotent(consumer: Path):
    profile = config.load_profile(consumer)
    sync.write(profile)
    agents = (consumer / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.startswith("# Consumer rules\n\nKeep these.\n")
    assert agents.count(sync.BEGIN) == 1 and agents.count(sync.END) == 1
    assert "vendor/loop-zero/adapters/codex.md" in agents
    (consumer / "AGENTS.md").write_text(agents + "\nOwner appended after the block.\n", encoding="utf-8")
    assert sync.check(profile) == [], "content outside the block never counts as drift"
    sync.write(profile)
    assert "Owner appended after the block." in (consumer / "AGENTS.md").read_text(encoding="utf-8")


def test_block_is_replaced_in_place_when_revision_changes(consumer: Path):
    profile = config.load_profile(consumer)
    sync.write(profile)
    new_revision = "f" * 40
    (consumer / "workflow.toml").write_text(
        (consumer / "workflow.toml").read_text().replace(profile.core_revision, new_revision)
    )
    profile = config.load_profile(consumer)
    assert Path("AGENTS.md") in sync.check(profile)
    sync.write(profile)
    text = (consumer / "AGENTS.md").read_text(encoding="utf-8")
    assert new_revision in text and profile.core_revision == new_revision
    assert text.count(sync.BEGIN) == 1


def test_generated_files_and_manifest(consumer: Path):
    profile = config.load_profile(consumer)
    sync.write(profile)
    fragment = (consumer / ".loopzero/claude-settings-fragment.json").read_text()
    assert '"hooks": {}' in fragment
    assert "kernel" in fragment and "make setup" not in fragment
    manifest = (consumer / ".loopzero/generated.json").read_text()
    assert ".cursor/rules/loopzero.mdc" in manifest and '"epoch": 1' in manifest


def test_symlinked_target_and_parent_are_refused(consumer: Path, tmp_path: Path):
    profile = config.load_profile(consumer)
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("outside\n")
    agents = consumer / "AGENTS.md"
    agents.unlink()
    agents.symlink_to(outside_file)
    with pytest.raises(config.ConfigError, match="symlink|outside"):
        sync.write(profile)
    assert outside_file.read_text() == "outside\n"

    agents.unlink()
    agents.write_text("owner\n")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (consumer / ".cursor").symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(config.ConfigError, match="symlink|outside"):
        sync.check(profile)
    assert list(outside_dir.iterdir()) == []


def test_target_outside_root_is_refused(consumer: Path, monkeypatch, tmp_path: Path):
    profile = config.load_profile(consumer)
    outside = tmp_path / "outside"
    monkeypatch.setattr(
        sync,
        "render",
        lambda _profile: [sync.Rendered(Path("../outside"), "bad\n", False)],
    )
    with pytest.raises(config.ConfigError, match="inside the repository"):
        sync.write(profile)
    assert not outside.exists()


@pytest.mark.parametrize(
    "contents",
    [
        f"{sync.BEGIN}\nold\n{sync.END}\n{sync.BEGIN}\nold\n{sync.END}\n",
        f"{sync.END}\nold\n{sync.BEGIN}\n",
        f"{sync.BEGIN}\nold\n",
        f"old\n{sync.END}\n",
        f"prefix {sync.BEGIN}\nold\n{sync.END}\n",
    ],
)
def test_malformed_markers_fail_without_writing(consumer: Path, contents: str):
    profile = config.load_profile(consumer)
    agents = consumer / "AGENTS.md"
    agents.write_text(contents)
    before = agents.read_bytes()
    with pytest.raises(config.ConfigError, match="marker"):
        sync.write(profile)
    assert agents.read_bytes() == before
    assert not (consumer / "CLAUDE.md").exists()


def test_late_marker_validation_prevents_earlier_target_write(consumer: Path):
    profile = config.load_profile(consumer)
    agents = consumer / "AGENTS.md"
    before = agents.read_bytes()
    (consumer / "CLAUDE.md").write_text(f"{sync.BEGIN}\nmissing end\n")
    with pytest.raises(config.ConfigError, match="marker"):
        sync.write(profile)
    assert agents.read_bytes() == before


def test_crlf_is_preserved(consumer: Path):
    profile = config.load_profile(consumer)
    agents = consumer / "AGENTS.md"
    agents.write_bytes(b"# Owner\r\n\r\nKeep this.\r\n")
    sync.write(profile)
    contents = agents.read_bytes()
    assert b"\r\n" in contents
    assert b"\n" not in contents.replace(b"\r\n", b"")
    assert sync.check(profile) == []


def test_block_at_top_and_missing_trailing_newline_are_idempotent(consumer: Path):
    profile = config.load_profile(consumer)
    agents = consumer / "AGENTS.md"
    agents.write_text(f"{sync.BEGIN}\nstale\n{sync.END}\nOwner\n")
    sync.write(profile)
    assert agents.read_text().startswith(sync.BEGIN)
    assert "Owner\n" in agents.read_text()
    assert sync.check(profile) == []

    agents.write_bytes(b"Owner without newline")
    sync.write(profile)
    assert agents.read_text().startswith("Owner without newline\n\n" + sync.BEGIN)
    assert sync.check(profile) == []


def test_concurrent_change_fails_before_replacing_any_target(
    consumer: Path, monkeypatch
):
    profile = config.load_profile(consumer)
    original_stage = sync._stage
    calls = 0

    def concurrent_stage(item: sync.Prepared, parent: sync.DirectoryHandle):
        nonlocal calls
        calls += 1
        temporary = original_stage(item, parent)
        if calls == 1:
            (consumer / "AGENTS.md").write_text("concurrent owner edit\n")
        return temporary

    monkeypatch.setattr(sync, "_stage", concurrent_stage)
    with pytest.raises(config.ConfigError, match="changed concurrently"):
        sync.write(profile)
    assert (consumer / "AGENTS.md").read_text() == "concurrent owner edit\n"
    assert not (consumer / "CLAUDE.md").exists()


def test_parent_exchange_after_validation_cannot_redirect_replacement(
    consumer: Path, monkeypatch, tmp_path: Path
):
    profile = config.load_profile(consumer)
    original_stage = sync._stage
    exchanged = False
    outside = tmp_path / "outside"
    outside.mkdir()

    def exchange_parent(item: sync.Prepared, parent: sync.DirectoryHandle):
        nonlocal exchanged
        temporary = original_stage(item, parent)
        if not exchanged:
            exchanged = True
            (consumer / ".cursor").rename(consumer / ".cursor-original")
            (consumer / ".cursor").symlink_to(outside, target_is_directory=True)
        return temporary

    monkeypatch.setattr(sync, "_stage", exchange_parent)
    with pytest.raises(config.ConfigError, match="symlink|changed concurrently"):
        sync.write(profile)
    assert list(outside.iterdir()) == []


def test_check_and_write_take_the_exclusive_sync_lock(
    consumer: Path, monkeypatch
):
    profile = config.load_profile(consumer)
    operations: list[int] = []
    real_flock = sync.fcntl.flock

    def recording_flock(descriptor: int, operation: int):
        operations.append(operation)
        return real_flock(descriptor, operation)

    monkeypatch.setattr(sync.fcntl, "flock", recording_flock)
    sync.check(profile)
    sync.write(profile)
    assert operations == [fcntl.LOCK_EX, fcntl.LOCK_EX]
    assert (consumer / sync.LOCK_PATH).is_file()


def test_sync_lock_blocks_a_second_process(consumer: Path):
    profile = config.load_profile(consumer)
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    finished = context.Event()
    process = context.Process(
        target=_check_in_separate_process,
        args=(str(consumer), started, finished),
    )
    with sync._locked_root(profile.root):
        process.start()
        assert started.wait(5)
        assert not finished.wait(0.25), "a second sync must wait for the repository lock"
    assert finished.wait(5)
    process.join(5)
    assert process.exitcode == 0


def test_interrupted_replace_leaves_partial_marker_until_recovery(
    consumer: Path, monkeypatch
):
    profile = config.load_profile(consumer)
    real_rename = sync.os.rename
    calls = 0

    def interrupted_rename(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated interruption")
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(sync.os, "rename", interrupted_rename)
    with pytest.raises(config.ConfigError, match="simulated interruption"):
        sync.write(profile)
    marker = consumer / sync.PARTIAL_PATH
    assert marker.is_file()
    assert sync.BEGIN in (consumer / "AGENTS.md").read_text()
    with pytest.raises(config.ConfigError, match="interrupted sync"):
        sync.check(profile)

    monkeypatch.setattr(sync.os, "rename", real_rename)
    sync.write(profile)
    assert not marker.exists()
    assert sync.check(profile) == []
