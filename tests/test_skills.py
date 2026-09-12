from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from conftest import REVISION, minimal_workflow
from loopzero import config, sync


REPO = Path(__file__).resolve().parents[1]
CORE_SKILLS = REPO / "core" / "skills"
INTELFLO = Path("/home/marcin/dev/intelflo")
MOVED_GOVERNANCE = {
    "align",
    "archive-docs",
    "audit-health",
    "backlog",
    "backlog-drain",
    "code-quality-drift",
    "coherence-audit",
    "commit-autofix",
    "debug",
    "decompose-module",
    "explain",
    "fix-failing-tests",
    "grill-me",
    "refine-code",
    "refine-design-doc",
    "remove-dead-code",
    "resolve-findings",
    "review-design-doc",
    "security-review",
    "skill-health",
    "work-issue",
    "write-design-doc",
    "write-tests",
}
CONSUMER_OWNED = {
    "audit-surface",
    "code-review",
    "design-handoff",
    "design-mockup",
    "execute-blueprint",
    "fix-ui-bug",
    "fortify-roadmap",
    "frontier-roadmap",
    "generate-parser-rules",
    "implement-backend",
    "implement-frontend",
    "review",
}
ALLOWED_FRONTMATTER = {"name", "description", "disable-model-invocation"}
TOKEN = re.compile(r"\{\{[a-z0-9_.]+\}\}")
MARKDOWN_LINK = re.compile(r"\]\(([^ )]+)(?: [^)]+)?\)")
REFERENCE_LINK = re.compile(r"^\s*\[[^]]+\]:\s*([^\s]+)", re.MULTILINE)


def _frontmatter(data: bytes) -> bytes:
    assert data.startswith(b"---\n")
    end = data.index(b"\n---\n", 4)
    return data[: end + 5]


def _skill_workflow() -> str:
    package = (
        "[package]\n"
        'env_prefix = "INTELFLO"\n'
        'product_name = "IntelFlo"\n'
        'primary_env = "INTELFLO_PRIMARY"\n'
        'audit_root = ".audit"\n'
        'skills_dir = ".cursor/skills"\n'
        'skill_mirrors = [".agents/skills", ".agent/skills", ".claude/skills"]\n'
        'docs_root = "../../../docs"\n'
        'rules_root = "../../rules"\n'
        'constraints_file = "../../../AGENTS.md"\n'
        'delivery_guide = "../../../docs/guides/dev-workflow/loop-zero-delivery.md"\n'
        'skills_readme = "../README.md"\n\n'
        'docs_dir = "docs"\n'
        'weekly_issue_workflow = ".github/workflows/weekly-issue.yml"\n'
        'openapi_document = "shared-data/openapi.json"\n'
        'commit_identity = "Vercel-authorized eowca"\n'
        'frontend_full_env = "CI_MIRROR_FULL_FRONTEND"\n\n'
        'suppressions_file = "../../../suppressions.yaml"\n\n'
        "[toolchain]\n"
        'backend_dir = "fastapi_backend"\n'
        'frontend_dir = "nextjs-frontend"\n'
        'scripts_dir = "scripts"\n'
        'scripts_root = "../../../scripts"\n\n'
        'python = "python3"\n'
        'system_python = "/usr/bin/python3"\n'
        'uv = "uv"\n'
        'pnpm = "pnpm"\n'
        'pytest = "pytest"\n'
        'vitest = "vitest"\n\n'
        "[toolchain.commands]\n"
    )
    commands = "".join(
        f'{name} = {json.dumps(value)}\n'
        for name, value in sorted(sync._COMMAND_DEFAULTS.items())
    )
    return minimal_workflow(extra=package + commands)


def test_intelflo_governance_render_is_byte_identical(tmp_path: Path) -> None:
    canonical = INTELFLO / ".cursor" / "skills"
    if not canonical.is_dir():
        pytest.skip(f"canonical IntelFlo skills unavailable at {canonical}")

    consumer = tmp_path / "intelflo-profile"
    consumer.mkdir()
    shutil.copytree(REPO / "core", consumer / "vendor" / "loop-zero")
    (consumer / "workflow.toml").write_text(_skill_workflow(), encoding="utf-8")
    for name in sorted(CONSUMER_OWNED):
        shutil.copytree(canonical / name, consumer / ".cursor" / "skills" / name)

    profile = config.load_profile(consumer)
    sync.write(profile)

    compared = 0
    for name in sorted(MOVED_GOVERNANCE):
        expected = (canonical / name / "SKILL.md").read_bytes()
        actual = (consumer / ".cursor" / "skills" / name / "SKILL.md").read_bytes()
        assert actual == expected, name
        assert _frontmatter(actual) == _frontmatter(expected), name
        mirror = INTELFLO / ".agents" / "skills" / name / "SKILL.md"
        if mirror.is_file():
            assert actual == mirror.read_bytes(), name
        compared += 1
    assert compared == 23
    for name in sorted(CONSUMER_OWNED):
        for source in (canonical / name).rglob("*"):
            if source.is_file():
                relative = source.relative_to(canonical / name)
                assert (consumer / ".cursor" / "skills" / name / relative).read_bytes() == source.read_bytes()


def test_skill_renderer_preserves_product_skills_and_builds_relative_mirrors(consumer: Path) -> None:
    local = consumer / ".cursor" / "skills" / "local-product"
    local.mkdir(parents=True)
    original = b"---\nname: local-product\ndescription: local\n---\n\nOwned.\n"
    (local / "SKILL.md").write_bytes(original)

    profile = config.load_profile(consumer)
    sync.write(profile)
    assert (local / "SKILL.md").read_bytes() == original
    manifest = json.loads((consumer / sync.SKILLS_MANIFEST_PATH).read_text())
    assert "local-product" in manifest["shadowed_consumer_skills"]
    assert "local-product" not in manifest["skills"]
    assert "tdd" in manifest["skills"]
    for mirror in profile.skill_mirrors:
        link = consumer / mirror / "local-product"
        assert link.is_symlink()
        assert os.readlink(link) == os.path.relpath(profile.skills_dir / "local-product", mirror)


def test_unmanifested_same_name_skill_is_never_overwritten(consumer: Path) -> None:
    local = consumer / ".cursor" / "skills" / "debug"
    local.mkdir(parents=True)
    original = b"---\nname: debug\ndescription: consumer override\n---\n\nKeep.\n"
    (local / "SKILL.md").write_bytes(original)

    profile = config.load_profile(consumer)
    sync.write(profile)
    assert (local / "SKILL.md").read_bytes() == original
    manifest = json.loads((consumer / sync.SKILLS_MANIFEST_PATH).read_text())
    assert "debug" in manifest["shadowed_consumer_skills"]
    assert "debug" not in manifest["skills"]


def test_sync_repairs_managed_skills_and_removes_only_stale_mirror_links(consumer: Path) -> None:
    profile = config.load_profile(consumer)
    sync.write(profile)
    debug = consumer / profile.skills_dir / "debug" / "SKILL.md"
    expected = debug.read_bytes()
    debug.write_text("managed drift\n", encoding="utf-8")
    stale = consumer / profile.skill_mirrors[0] / "removed-product"
    stale.symlink_to("../../.cursor/skills/removed-product")
    owner_file = consumer / profile.skill_mirrors[0] / "owner-note"
    owner_file.write_text("keep\n", encoding="utf-8")

    drift = sync.check(profile)
    assert debug.relative_to(consumer) in drift
    assert stale.relative_to(consumer) in drift
    sync.write(profile)
    assert debug.read_bytes() == expected
    assert not stale.exists() and not stale.is_symlink()
    assert owner_file.read_text() == "keep\n"


def test_vendored_pins_and_upstream_skill_bytes_are_exact() -> None:
    upstream = Path("/tmp/loopzero-coord/mattpocock-skills-a5")
    expected = {
        "grill-with-docs": "7de372c13488f1ee96cc11cd8907b56b6809cc93eef776eeddd37de6b6cbe3fe",
        "tdd": "cb01f66bebfaa25fa1f88e6b7e769cd9fd9f35b1120b8563749820738814c927",
        "code-review": "47f4e52c21694def9c7c11cbfbf891ca35eac7a93e395797515be3c8a409ae50",
    }
    for name, digest in expected.items():
        data = (CORE_SKILLS / "vendor" / name / "SKILL.md").read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        pin = (CORE_SKILLS / "vendor" / name / "PIN").read_text()
        assert "3cca18b368ae95cdbdebbff572ccafa662551015" in pin
        assert "license: MIT" in pin
        if upstream.is_dir():
            assert data == (upstream / "skills" / "engineering" / name / "SKILL.md").read_bytes()


def test_skill_budgets_frontmatter_tokens_and_worktree_policy() -> None:
    hard_skill = 300
    hard_reference = 150
    hard_combined = 400
    exempt = {
        "align",
        "audit-health",
        "archive-docs",
        "backlog",
        "code-quality-drift",
        "commit-autofix",
        "diagnose",
        "explain",
        "grill-me",
        "implement",
        "plan",
        "remove-dead-code",
        "resolve-findings",
        "review",
        "review-design-doc",
        "security-review",
    }
    for directory in sorted(path for path in CORE_SKILLS.iterdir() if path.is_dir() and path.name != "vendor"):
        skill = directory / "SKILL.md"
        skill_lines = len(skill.read_text(encoding="utf-8").splitlines())
        assert skill_lines <= hard_skill, directory.name
        reference = directory / "reference.md"
        reference_lines = len(reference.read_text(encoding="utf-8").splitlines()) if reference.exists() else 0
        assert reference_lines <= hard_reference, directory.name
        assert skill_lines + reference_lines <= hard_combined, directory.name

        frontmatter = _frontmatter(skill.read_bytes()).decode("utf-8")
        keys = {
            match.group(1)
            for match in re.finditer(r"^([a-z_-]+):", frontmatter, re.MULTILINE)
        }
        assert keys <= ALLOWED_FRONTMATTER, directory.name
        assert f"name: {directory.name}" in frontmatter
        assert not TOKEN.search(frontmatter), directory.name
        if directory.name not in exempt:
                text = skill.read_text(encoding="utf-8").lower()
                assert "worktree" in text, directory.name

    for directory in sorted(path for path in (CORE_SKILLS / "vendor").iterdir() if path.is_dir()):
        skill = directory / "SKILL.md"
        skill_lines = len(skill.read_text(encoding="utf-8").splitlines())
        supporting_lines = sum(
            len(path.read_text(encoding="utf-8").splitlines())
            for path in directory.glob("*.md")
            if path.name not in {"SKILL.md", "OVERLAY.md"}
        )
        assert skill_lines <= hard_skill, directory.name
        assert supporting_lines <= hard_reference, directory.name
        assert skill_lines + supporting_lines <= hard_combined, directory.name
        frontmatter = _frontmatter(skill.read_bytes()).decode("utf-8")
        keys = {
            match.group(1)
            for match in re.finditer(r"^([a-z_-]+):", frontmatter, re.MULTILINE)
        }
        assert keys <= ALLOWED_FRONTMATTER, directory.name
        assert f"name: {directory.name}" in frontmatter


def test_local_skill_links_resolve_or_name_a_classified_consumer_skill() -> None:
    classified = set(re.findall(r"^\| `([^`]+)` \|", (CORE_SKILLS / "CLASSIFICATION.md").read_text(), re.MULTILINE))
    sources = [*CORE_SKILLS.glob("*/*.md"), *CORE_SKILLS.glob("vendor/*/*.md")]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for target in [*MARKDOWN_LINK.findall(text), *REFERENCE_LINK.findall(text)]:
            target = target.strip("<>").split("#", 1)[0]
            if not target or "://" in target or "{{" in target:
                continue
            resolved = source.parent / target
            if resolved.exists():
                continue
            match = re.fullmatch(r"\.\./([^/]+)/SKILL\.md", target)
            assert match and match.group(1) in classified, (source, target)


def test_decision_and_review_skill_schema_vocabulary() -> None:
    for name in ("grill-me", "resolve-findings"):
        text = (CORE_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        for token in ("Locked", "Assumed", "Deferred", "id", "un-routed", "agent_event"):
            assert token.lower() in text.lower(), (name, token)
    for name in ("review-design-doc", "security-review"):
        text = (CORE_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        for token in ("critical", "important", "suggestion", "agent_event", "cross-harness"):
            assert token in text, (name, token)
