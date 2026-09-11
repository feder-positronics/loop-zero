from pathlib import Path

import pytest

from loopzero import config, sync


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
    (consumer / ".loopzero").symlink_to(outside_dir, target_is_directory=True)
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
    original_read = sync._read_target
    calls = 0

    def concurrent_read(target: Path):
        nonlocal calls
        calls += 1
        if calls == 6:
            (consumer / "AGENTS.md").write_text("concurrent owner edit\n")
        return original_read(target)

    monkeypatch.setattr(sync, "_read_target", concurrent_read)
    with pytest.raises(config.ConfigError, match="changed concurrently"):
        sync.write(profile)
    assert (consumer / "AGENTS.md").read_text() == "concurrent owner edit\n"
    assert not (consumer / "CLAUDE.md").exists()
