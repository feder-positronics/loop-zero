from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urldefrag, urlsplit

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
INLINE_LINK = re.compile(
    r"\]\(\s*(?P<target><[^>]+>|[^\s)]+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
REFERENCE_LINK = re.compile(
    r"^\s*\[[^]]+\]:\s*(?P<target><[^>]+>|[^\s]+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*$"
)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
EXPLICIT_ANCHOR = re.compile(
    r"<(?:a\s+(?:name|id)|[^>]+\s+id)=[\"']([^\"']+)[\"']", re.IGNORECASE
)


def _frontmatter(data: bytes) -> bytes:
    assert data.startswith(b"---\n")
    end = data.index(b"\n---\n", 4)
    return data[: end + 5]


def _markdown_targets(text: str):
    fence_character = ""
    fence_length = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        fence_match = re.match(r"(?P<fence>`{3,}|~{3,})", stripped)
        if fence_match:
            marker = fence_match.group("fence")
            if not fence_character:
                fence_character, fence_length = marker[0], len(marker)
            elif marker[0] == fence_character and len(marker) >= fence_length:
                fence_character, fence_length = "", 0
            continue
        if fence_character:
            continue
        prose = re.sub(r"`[^`]*`", "", line)
        yield from (match.group("target").strip("<>") for match in INLINE_LINK.finditer(prose))
        reference = REFERENCE_LINK.match(prose)
        if reference:
            yield reference.group("target").strip("<>")


def _slugify_heading(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text.lower().strip())
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text).strip("-")


def _anchors(path: Path) -> set[str]:
    if path.suffix not in {".md", ".mdc"}:
        return set()
    body = path.read_text(encoding="utf-8")
    return {
        *(_slugify_heading(match.group(1)) for match in HEADING.finditer(body)),
        *(match.group(1) for match in EXPLICIT_ANCHOR.finditer(body)),
    }


def _intelflo_token_block() -> str:
    return '''[skill_tokens]
base_branch = "origin/main"
coherence_owner = "Marcin"
doctrine_d2 = "D-2"
doctrine_d14 = "D-14"
doctrine_d16 = "D-16"
doctrine_d18 = "D-18"
doctrine_d21 = "D-21"
legacy_delivery_contract = """
For a new task, use [loop-zero delivery](../../../docs/guides/dev-workflow/loop-zero-delivery.md)
under `AGENTS.md`'s Primary Delivery Contract. Retain issue acceptance and, when
applicable, blueprint lifecycle commands. The legacy procedure below applies
only to runs whose original record selects `intelflo-v1`; never migrate an
existing run, reset its history, or apply its phase choreography to a new task."""
debug_environment = """
- Backend API, SQLAlchemy, and error handling:
  [backend patterns](../../../docs/guides/backend/backend-patterns.md).
- Backend tests and the port-5433 recovery:
  [backend testing](../../../docs/guides/backend/backend-testing.md).
- UI state/network triage and isolated-stack evidence:
  [UI debugging](../../../docs/guides/frontend/bug-hunting-debug.md).
- Collaborative T3 inspection and bounded recovery:
  [T3 preview readiness and browser authority](../../../docs/guides/frontend/frontend-testing-e2e.md#preview-readiness-and-bounded-recovery).
- Frontend tests:
  [frontend testing](../../../docs/guides/frontend/frontend-testing.md).
- Production frontend and Vercel inspection:
  [production frontend](../../../docs/ops/runbooks/production-frontend-debugging.md)
  and [Vercel CLI](../../../docs/ops/runbooks/vercel-cli-guide.md)."""
commit_hook_chain = """
This repo has **two** pre-commit pipelines that `git commit` runs:

1. **pre-commit** (Python hooks via fastapi_backend venv):
   ```bash
   cd fastapi_backend && uv run pre-commit run
   ```
   Hooks: trailing-whitespace, ruff (lint + format), prettier, openapi-generate,
   sync-agent-rules, export-requirements, pytest-config-check,
   service-organization-check, alembic-heads-check, docs index generation,
   check-skills-canonical-dir.

2. **lint-staged** (frontend hooks via simple-git-hooks in `nextjs-frontend/package.json`):
   ```bash
   pnpm -C nextjs-frontend lint-staged
   ```
   Runs only when TS/TSX files are staged; `pre-commit run` does NOT trigger it.
   The commit-autofix script runs it after pre-commit so it validates the final
   staged TS/TSX state, including generated SDK files."""
commit_dependency_preflight = """If `nextjs-frontend/package.json` or `pnpm-lock.yaml` changed after a branch switch, merge, or cherry-pick, refresh deps before frontend validation: `pnpm -C nextjs-frontend install --frozen-lockfile`. In worktrees, a missing `lint-staged` is self-healed when staged TS/TSX or docs files require the shared dependency tree (`commit-auto-fix.sh` runs `make worktree-setup`; the runtime-entry hook pre-runs it). Backend-only commits do not need frontend dependency setup. Manual fix only if self-heal fails: `make worktree-setup` (worktree) / `pnpm -C nextjs-frontend install` (primary). Frontend `test` scripts fail fast on a stale Vitest binary; verify with `pnpm -C nextjs-frontend exec vitest --version` when needed."""
commit_mirror_paths = """| `.cursor/rules/*.mdc`, `.cursor/skills/*/SKILL.md` | `.agents/rules/*.md`, `.agent/rules/*`, `.claude/rules/*` |"""
commit_backend_hook_behavior = """
**Backend source changed** (`fastapi_backend/`): backend test-lane selection
(tooling vs full unit vs risk critical) follows the ladder in the AGENTS.md
Quick Commands table; `commit-auto-fix.sh` is the deterministic authority. Do
**not** substitute `make test-be` or `make test-be-slow` — those are broader
regression lanes, not commit-hook checks.

The full-unit xdist lane (`make test-be-precommit-unit`) is a convenience check, not the git pre-commit gate (staged-file hooks). It flakes under load — workers crash, or **diff-unrelated** tests fail (different set each run, pass in isolation). A failure outside the staged diff that passes alone is xdist pollution: re-run once (lower `PYTEST_WORKERS` if workers crashed), don't debug it (`reference_precommit_xdist_flakiness`).

**Agent-tooling tests only** (`fastapi_backend/tests/unit/scripts/`, with any
matching root `scripts/` implementation): `make test-agent-tooling`.
This lane runs the changed owning workflow-script tests (or the complete
workflow-script surface when no owning test is in scope) and skips the product
unit suite. Any product test, backend app, dependency, config, or shared test
infrastructure path keeps the full-unit fail-safe routing."""

[skill_routes]
audit_surface = "audit-surface"
design_handoff = "design-handoff"
design_mockup = "design-mockup"
execute_blueprint = "execute-blueprint"
fix_ui_bug = "fix-ui-bug"
fortify_roadmap = "fortify-roadmap"
frontier_roadmap = "frontier-roadmap"
generate_parser_rules = "generate-parser-rules"
implement_backend = "implement-backend"
implement_frontend = "implement-frontend"

'''


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
        + _intelflo_token_block()
        + "[toolchain]\n"
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
    return minimal_workflow(extra=package + commands, include_identity=False)


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
        expected_directory = canonical / name
        actual_directory = consumer / ".cursor" / "skills" / name
        expected_paths = {
            path.relative_to(expected_directory)
            for path in expected_directory.rglob("*")
            if path.is_file()
        }
        actual_paths = {
            path.relative_to(actual_directory)
            for path in actual_directory.rglob("*")
            if path.is_file()
        }
        assert actual_paths == expected_paths, name
        for relative in sorted(expected_paths):
            assert (actual_directory / relative).read_bytes() == (
                expected_directory / relative
            ).read_bytes(), (name, relative)
        expected = (expected_directory / "SKILL.md").read_bytes()
        actual = (actual_directory / "SKILL.md").read_bytes()
        assert actual == expected, name
        assert _frontmatter(actual) == _frontmatter(expected), name
        mirror = INTELFLO / ".agents" / "skills" / name / "SKILL.md"
        if mirror.is_file():
            assert actual == mirror.read_bytes(), name
        compared += 1
    assert compared == 23
    print(f"{compared} identical")
    for name in sorted(CONSUMER_OWNED):
        for source in (canonical / name).rglob("*"):
            if source.is_file():
                relative = source.relative_to(canonical / name)
                assert (consumer / ".cursor" / "skills" / name / relative).read_bytes() == source.read_bytes()


def test_documented_intelflo_token_block_is_the_byte_identity_profile() -> None:
    readme = (CORE_SKILLS / "README.md").read_text(encoding="utf-8")
    documented = readme.split("````toml\n", 1)[1].split("\n````", 1)[0]
    assert documented == _intelflo_token_block().strip()


def test_skill_renderer_preserves_product_skills_and_builds_relative_mirrors(consumer: Path) -> None:
    local = consumer / ".cursor" / "skills" / "local-product"
    local.mkdir(parents=True)
    original = b"---\nname: local-product\ndescription: local\n---\n\nOwned.\n"
    (local / "SKILL.md").write_bytes(original)

    profile = config.load_profile(consumer)
    sync.write(profile)
    assert (local / "SKILL.md").read_bytes() == original
    manifest = json.loads((consumer / sync.SKILLS_MANIFEST_PATH).read_text())
    assert manifest["schema_version"] == 2
    assert manifest["skills_dir"] == profile.skills_dir.as_posix()
    assert manifest["canonical_files"][f"{profile.skills_dir}/README.md"]["type"] == "regular_file"
    debug_link = f"{profile.skill_mirrors[0]}/debug"
    assert manifest["mirror_links"][debug_link] == {
        "type": "symlink",
        "target": os.path.relpath(profile.skills_dir / "debug", profile.skill_mirrors[0]),
    }
    assert "local-product" in manifest["shadowed_consumer_skills"]
    assert "local-product" not in manifest["skills"]
    assert "tdd" in manifest["skills"]
    for mirror in profile.skill_mirrors:
        link = consumer / mirror / "local-product"
        assert link.is_symlink()
        assert os.readlink(link) == os.path.relpath(profile.skills_dir / "local-product", mirror)


@pytest.mark.parametrize("missing", ["product_name", "commit_identity"])
def test_identity_tokens_have_no_package_defaults(consumer: Path, missing: str) -> None:
    workflow = consumer / "workflow.toml"
    lines = [
        line
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{missing} = ")
    ]
    workflow.write_text("\n".join(lines) + "\n", encoding="utf-8")
    profile = config.load_profile(consumer)
    with pytest.raises(config.ConfigError, match=rf"\[package\]\.{missing}: required"):
        sync.render(profile)


def test_missing_product_routes_render_explicitly_unavailable(consumer: Path) -> None:
    profile = config.load_profile(consumer)
    sync.write(profile)
    debug = (consumer / profile.skills_dir / "debug" / "SKILL.md").read_text()
    assert "`fix-ui-bug` (route unavailable in this consumer)" in debug
    assert "../fix-ui-bug/SKILL.md" not in debug


def test_unknown_skill_command_token_is_rejected(consumer: Path) -> None:
    workflow = consumer / "workflow.toml"
    workflow.write_text(
        workflow.read_text() + '\n[toolchain.commands]\ntest_backend_unti = "make typo"\n',
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError, match="test_backend_unti.*unknown"):
        config.load_profile(consumer)


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


def test_sync_refuses_managed_drift_and_preserves_unmanifested_mirror_links(consumer: Path) -> None:
    profile = config.load_profile(consumer)
    sync.write(profile)
    debug = consumer / profile.skills_dir / "debug" / "SKILL.md"
    debug.write_text("managed drift\n", encoding="utf-8")
    owner_link = consumer / profile.skill_mirrors[0] / "removed-product"
    owner_link.symlink_to("../../.cursor/skills/removed-product")
    owner_file = consumer / profile.skill_mirrors[0] / "owner-note"
    owner_file.write_text("keep\n", encoding="utf-8")

    with pytest.raises(config.ConfigError, match="digest changed"):
        sync.check(profile)
    with pytest.raises(config.ConfigError, match="digest changed"):
        sync.write(profile)
    assert debug.read_text() == "managed drift\n"
    assert owner_link.is_symlink()
    assert owner_file.read_text() == "keep\n"


def test_unmanifested_stale_mirror_link_is_not_drift_or_removed(consumer: Path) -> None:
    profile = config.load_profile(consumer)
    sync.write(profile)
    owner_link = consumer / profile.skill_mirrors[0] / "removed-product"
    owner_link.symlink_to("../../.cursor/skills/removed-product")

    assert sync.check(profile) == []
    assert sync.write(profile) == []
    assert owner_link.is_symlink()


def test_consumer_regular_file_at_desired_mirror_name_is_refused(consumer: Path) -> None:
    profile = config.load_profile(consumer)
    target = consumer / profile.skill_mirrors[0] / "debug"
    target.parent.mkdir(parents=True)
    target.write_text("consumer-owned\n", encoding="utf-8")

    with pytest.raises(config.ConfigError, match="consumer-owned regular file"):
        sync.write(profile)
    assert target.read_text() == "consumer-owned\n"
    assert not (consumer / profile.skills_dir / "debug").exists()


def test_new_snapshot_file_cannot_replace_unmanifested_file_in_managed_skill(
    consumer: Path,
) -> None:
    profile = config.load_profile(consumer)
    sync.write(profile)
    target = consumer / profile.skills_dir / "debug" / "new.md"
    target.write_text("consumer-owned\n", encoding="utf-8")
    source = consumer / profile.core_path / "skills" / "debug" / "new.md"
    source.write_text("new snapshot content\n", encoding="utf-8")

    with pytest.raises(config.ConfigError, match="unmanifested file is consumer-owned"):
        sync.write(profile)
    assert target.read_text() == "consumer-owned\n"


def test_changing_skills_dir_does_not_transfer_old_name_ownership(consumer: Path) -> None:
    profile = config.load_profile(consumer)
    sync.write(profile)
    workflow = consumer / "workflow.toml"
    workflow.write_text(
        workflow.read_text().replace(
            'commit_identity = "test-only commit identity"',
            'commit_identity = "test-only commit identity"\nskills_dir = ".other/skills"',
        ),
        encoding="utf-8",
    )
    local = consumer / ".other/skills/debug"
    local.mkdir(parents=True)
    owned = local / "SKILL.md"
    owned.write_text("consumer-owned\n", encoding="utf-8")

    changed_profile = config.load_profile(consumer)
    sync.write(changed_profile)
    assert owned.read_text() == "consumer-owned\n"
    manifest = json.loads((consumer / sync.SKILLS_MANIFEST_PATH).read_text())
    assert manifest["skills_dir"] == ".other/skills"
    assert "debug" in manifest["shadowed_consumer_skills"]


def test_planted_manifest_digest_cannot_claim_consumer_skill(consumer: Path) -> None:
    local = consumer / ".cursor/skills/review"
    local.mkdir(parents=True)
    owned = local / "SKILL.md"
    owned.write_text("consumer-owned\n", encoding="utf-8")
    manifest = {
        "core_revision": REVISION,
        "skills_dir": ".cursor/skills",
        "skill_mirrors": [],
        "skills": {
            "review": {
                "source": "governance",
                "files": ["SKILL.md"],
                "sha256": {"SKILL.md": "0" * 64},
            }
        },
        "shadowed_consumer_skills": [],
        "mirrored_skills": [],
    }
    path = consumer / sync.SKILLS_MANIFEST_PATH
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(config.ConfigError, match="digest changed"):
        sync.write(config.load_profile(consumer))
    assert owned.read_text() == "consumer-owned\n"


def test_removed_snapshot_skill_cleans_manifested_files_links_and_directory(
    consumer: Path,
) -> None:
    profile = config.load_profile(consumer)
    sync.write(profile)
    canonical = consumer / profile.skills_dir / "debug"
    mirrors = [consumer / mirror / "debug" for mirror in profile.skill_mirrors]
    shutil.rmtree(consumer / profile.core_path / "skills" / "debug")

    changed = sync.write(profile)
    assert not canonical.exists()
    assert all(not path.exists() and not path.is_symlink() for path in mirrors)
    assert canonical.relative_to(consumer) not in sync.check(profile)
    assert any(path.name == "SKILL.md" for path in changed)


def test_reserved_overlay_delimiters_and_malformed_placeholders_are_rejected(
    consumer: Path,
) -> None:
    profile = config.load_profile(consumer)
    vendor = consumer / profile.core_path / "skills/vendor/tdd/SKILL.md"
    vendor.write_text(vendor.read_text() + f"\n{sync.SKILL_OVERLAY_END}\n")
    with pytest.raises(config.ConfigError, match="reserved overlay delimiter"):
        sync.render(profile)

    vendor.write_bytes((CORE_SKILLS / "vendor/tdd/SKILL.md").read_bytes())
    governance = consumer / profile.core_path / "skills/align/SKILL.md"
    governance.write_text(governance.read_text() + "\n{{ package.product_name }}\n")
    with pytest.raises(config.ConfigError, match="malformed skill template"):
        sync.render(profile)


def test_vendored_pins_and_upstream_skill_bytes_are_exact() -> None:
    upstream = Path("/tmp/loopzero-coord/mattpocock-skills-a5")
    expected = {
        "grill-with-docs/SKILL.md": "7de372c13488f1ee96cc11cd8907b56b6809cc93eef776eeddd37de6b6cbe3fe",
        "tdd/SKILL.md": "cb01f66bebfaa25fa1f88e6b7e769cd9fd9f35b1120b8563749820738814c927",
        "tdd/mocking.md": "3ceb807fdf4a47d6a93d4d9a891e5ba6d362a6247bd08adc451feebfc17361ef",
        "tdd/tests.md": "859f9e592c188fda4fc7277dd180e4ce9c7a2e13f6efe1f6f29eccc9d28c106a",
        "code-review/SKILL.md": "47f4e52c21694def9c7c11cbfbf891ca35eac7a93e395797515be3c8a409ae50",
    }
    for relative, digest in expected.items():
        data = (CORE_SKILLS / "vendor" / relative).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        name = relative.split("/", 1)[0]
        pin = (CORE_SKILLS / "vendor" / name / "PIN").read_text()
        assert "3cca18b368ae95cdbdebbff572ccafa662551015" in pin
        assert "license: MIT" in pin
        if upstream.is_dir():
            assert data == (upstream / "skills" / "engineering" / relative).read_bytes()


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
        assert "{{package." not in frontmatter, directory.name
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

    orchestrators = {"work-issue"}
    dispatch_contract = re.compile(
        r"pwd\s*&&\s*git\s+rev-parse\s+--show-toplevel|verification triple|Subagent Dispatch",
        re.IGNORECASE,
    )
    for name in orchestrators:
        text = (CORE_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert dispatch_contract.search(text), name


def test_rendered_example_skill_links_and_anchors_resolve(tmp_path: Path) -> None:
    consumer = tmp_path / "example"
    shutil.copytree(REPO / "tests" / "example_consumer", consumer)
    shutil.copytree(REPO / "core", consumer / "vendor" / "loop-zero")
    profile = config.load_profile(consumer)
    sync.write(profile)

    skill_root = consumer / profile.skills_dir
    checked = 0
    for source in sorted(skill_root.rglob("*.md")):
        for raw_target in _markdown_targets(source.read_text(encoding="utf-8")):
            parsed = urlsplit(unquote(raw_target))
            if parsed.scheme or parsed.netloc:
                continue
            path_part, fragment = urldefrag(unquote(raw_target))
            if not path_part:
                resolved = source
            elif path_part.startswith("/"):
                continue
            else:
                resolved = (source.parent / path_part).resolve()
            assert resolved.exists(), (source.relative_to(consumer), raw_target)
            if fragment and resolved.is_file():
                assert fragment in _anchors(resolved), (
                    source.relative_to(consumer),
                    raw_target,
                )
            checked += 1
    assert checked > 100


def test_decision_and_review_skill_schema_vocabulary() -> None:
    for name in ("grill-me", "resolve-findings"):
        text = (CORE_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        for token in ("Locked", "Assumed", "Deferred", "id", "un-routed", "agent_event"):
            assert token.lower() in text.lower(), (name, token)
    for name in ("review-design-doc", "security-review"):
        text = (CORE_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        for token in ("critical", "important", "suggestion", "agent_event", "cross-harness"):
            assert token in text, (name, token)
