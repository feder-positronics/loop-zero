from pathlib import Path

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
    assert "EnterWorktree" in fragment and "make setup" in fragment
    manifest = (consumer / ".loopzero/generated.json").read_text()
    assert ".cursor/rules/loopzero.mdc" in manifest and '"epoch": 1' in manifest
