"""Immutable Git fixtures exercise complete-tree republication proofs."""

import subprocess
import sys
from pathlib import Path

import pytest

from loopzero.delivery import _publish_equivalence as equivalence
from loopzero.delivery._publish_equivalence import prove_republish_equivalence


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, str, str, str]:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.test")
    git(tmp_path, "config", "core.hooksPath", "/dev/null")
    base = commit(tmp_path, "author.txt", "original\n")
    git(tmp_path, "checkout", "-qb", "old")
    old = commit(tmp_path, "author.txt", "author change\n")
    git(tmp_path, "checkout", "-q", "main")
    new_base = commit(tmp_path, "upstream.txt", "base movement\n")
    git(tmp_path, "checkout", "-qb", "rebased")
    git(tmp_path, "cherry-pick", old)
    return tmp_path, old, new_base, base


def test_complete_author_replay_accepts_rebase(history) -> None:
    repo, old, base, _ = history
    head = git(repo, "rev-parse", "HEAD")
    proof = prove_republish_equivalence(
        repo=repo, observed_head=old, expected_head=head, base_head=base
    )
    assert proof is not None
    assert proof["observed_head"] == old
    assert proof["expected_head"] == head
    assert proof["result_tree_sha"] == git(repo, "rev-parse", "HEAD^{tree}")


@pytest.mark.parametrize("change", ["extra", "missing", "mode", "binary"])
def test_complete_author_replay_rejects_any_unproved_change(history, change) -> None:
    repo, old, base, _ = history
    if change == "extra":
        commit(repo, "unrelated.txt", "unreviewed\n")
    elif change == "missing":
        commit(repo, "author.txt", "original\n")
    elif change == "mode":
        git(repo, "update-index", "--chmod=+x", "author.txt")
        git(repo, "commit", "-qm", "mode drift")
    else:
        commit(repo, "binary", "\x00unreviewed")
    assert (
        prove_republish_equivalence(
            repo=repo,
            observed_head=old,
            expected_head=git(repo, "rev-parse", "HEAD"),
            base_head=base,
        )
        is None
    )


def test_identical_tree_accepts_rewritten_commit(history) -> None:
    repo, old, _, old_base = history
    git(repo, "checkout", "-q", "old")
    git(repo, "commit", "--amend", "-qm", "rewritten message")
    proof = prove_republish_equivalence(
        repo=repo,
        observed_head=old,
        expected_head=git(repo, "rev-parse", "HEAD"),
        base_head=old_base,
    )
    assert proof is not None
    assert proof["method"] == "identical-tree"


def test_unknown_object_fails_closed(history) -> None:
    repo, _, base, _ = history
    assert (
        prove_republish_equivalence(
            repo=repo,
            observed_head="f" * 40,
            expected_head=git(repo, "rev-parse", "HEAD"),
            base_head=base,
        )
        is None
    )


def generate_index(repo: Path) -> None:
    # Separate process prevents fixture generation from hiding lazy-import defects.
    docs_scripts = Path(equivalence.__file__).resolve().parent / "_docs"
    subprocess.check_call(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
            "import generate_indexes as g; g.ROOT=Path(sys.argv[2]); "
            "g.process_directory(g.ROOT/'docs/design/explorations', 'index.md', "
            "'managed', scan_mode='direct')",
            str(docs_scripts),
            str(repo),
        ]
    )


@pytest.fixture
def generated_history(history) -> tuple[Path, str, str]:
    repo, _, _, original_base = history
    git(repo, "checkout", "-qb", "generated-base", original_base)
    directory = repo / "docs/design/explorations"
    directory.mkdir(parents=True)
    (directory / "index.md").write_text(
        "# Explorations\n\n<!-- .generated-index-start -->\n"
        "<!-- .generated-index-end -->\n"
    )
    generate_index(repo)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "generated base")
    git(repo, "checkout", "-qb", "generated-old")
    author = "---\nstatus: draft\n---\n# Author document\n"
    (directory / "2026-09-05-author.md").write_text(author)
    generate_index(repo)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "author document")
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "generated-base")
    (directory / "2026-09-06-upstream.md").write_text(
        "---\nstatus: draft\n---\n# Upstream document\n"
    )
    generate_index(repo)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "upstream document")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "generated-new")
    (directory / "2026-09-05-author.md").write_text(author)
    generate_index(repo)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "author with regenerated union")
    return repo, old, base


def test_generated_index_union_is_proven_by_regeneration(generated_history) -> None:
    repo, old, base = generated_history
    before = git(repo, "status", "--porcelain=v1")
    index_before = (repo / ".git/index").read_bytes()
    proof = prove_republish_equivalence(
        repo=repo,
        observed_head=old,
        expected_head=git(repo, "rev-parse", "HEAD"),
        base_head=base,
    )
    assert proof is not None
    assert proof["method"] == "regenerated-author-replay"
    assert len(proof["regeneration"]) == 2
    assert git(repo, "status", "--porcelain=v1") == before
    assert (repo / ".git/index").read_bytes() == index_before


@pytest.mark.parametrize(
    "drift", ["managed", "handwritten", "missing-author", "extra", "mode", "symlink"]
)
def test_generated_divergence_does_not_hide_author_drift(
    generated_history, drift
) -> None:
    repo, old, base = generated_history
    target = repo / equivalence.INDEX
    if drift == "managed":
        target.write_text(target.read_text().replace("| Date |", "| Forged |"))
    elif drift == "handwritten":
        target.write_text(
            target.read_text().replace("# Explorations", "# Changed prose")
        )
    elif drift == "missing-author":
        (target.parent / "2026-09-05-author.md").unlink()
        generate_index(repo)
    elif drift == "extra":
        (repo / "unreviewed.txt").write_text("extra change\n")
    elif drift == "mode":
        target.chmod(0o755)
    else:
        (target.parent / "escape.md").symlink_to("/etc/passwd")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "unproved drift")
    assert (
        prove_republish_equivalence(
            repo=repo,
            observed_head=old,
            expected_head=git(repo, "rev-parse", "HEAD"),
            base_head=base,
        )
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "candidate-controlled"),
        ("path", "[candidate-controlled]"),
        ("path", "{value: candidate-controlled}"),
        ("name", "[x]"),
        ("name", "{value: x}"),
        ("name", "\n  - x"),
        ("name", "\n  value: x"),
        ("title", "[x]"),
        ("title", "{value: x}"),
    ],
)
def test_malformed_frontmatter_fails_closed(generated_history, field, value) -> None:
    repo, old, base = generated_history
    head = commit(
        repo,
        "docs/design/explorations/2026-09-05-author.md",
        f"---\nstatus: draft\n{field}: {value}\n---\n# Author document\n",
    )
    before = git(repo, "status", "--porcelain=v1")
    index_before = (repo / ".git/index").read_bytes()
    assert (
        prove_republish_equivalence(
            repo=repo, observed_head=old, expected_head=head, base_head=base
        )
        is None
    )
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain=v1") == before
    assert (repo / ".git/index").read_bytes() == index_before


def test_unavailable_generator_fails_closed(generated_history, monkeypatch) -> None:
    repo, old, base = generated_history

    def unavailable(*args):
        raise ImportError("trusted generator dependency unavailable")

    monkeypatch.setattr(equivalence, "_regenerate", unavailable)
    assert (
        prove_republish_equivalence(
            repo=repo,
            observed_head=old,
            expected_head=git(repo, "rev-parse", "HEAD"),
            base_head=base,
        )
        is None
    )


def test_regeneration_never_executes_candidate_generator(generated_history) -> None:
    repo, old, base = generated_history
    marker = repo / "candidate-executed"
    source = "scripts/docs/generate_indexes.py"
    payload = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    git(repo, "checkout", "-q", "generated-old")
    (repo / source).parent.mkdir(parents=True)
    old = commit(repo, source, payload)
    git(repo, "checkout", "-q", "generated-new")
    (repo / source).parent.mkdir(parents=True, exist_ok=True)
    head = commit(repo, source, payload)
    proof = prove_republish_equivalence(
        repo=repo, observed_head=old, expected_head=head, base_head=base
    )
    assert proof is not None
    assert proof["method"] == "regenerated-author-replay"
    assert not marker.exists()


def test_trusted_import_path_covers_scan_and_is_restored(
    generated_history, monkeypatch
) -> None:
    repo, old, base = generated_history
    original_spec = equivalence.importlib.util.spec_from_file_location
    before = sys.path.copy()
    scans = []

    def guarded_spec(name, path):
        spec = original_spec(name, path)
        original_exec = spec.loader.exec_module

        def load(generator):
            original_exec(generator)
            original_collect = generator.collect_entries

            def scan(*args):
                assert sys.path[0] == str(Path(path).parent)
                scans.append(True)
                return original_collect(*args)

            generator.collect_entries = scan

        spec.loader.exec_module = load
        return spec

    monkeypatch.setattr(
        equivalence.importlib.util, "spec_from_file_location", guarded_spec
    )
    assert (
        prove_republish_equivalence(
            repo=repo,
            observed_head=old,
            expected_head=git(repo, "rev-parse", "HEAD"),
            base_head=base,
        )
        is not None
    )
    assert scans == [True, True]
    assert sys.path == before


@pytest.mark.parametrize("failure", [None, "equivalence", "concurrent"])
def test_real_remote_lease_preserves_main_and_concurrent_head(history, failure) -> None:
    from loopzero.delivery import publish as pr_publish

    repo, old, base, _ = history
    expected = git(repo, "rev-parse", "HEAD")
    if failure == "equivalence":
        expected = commit(repo, "extra.txt", "unproved edit\n")
    remote = repo.parent / (repo.name + "-remote.git")
    subprocess.check_call(["git", "init", "--bare", "-q", str(remote)])
    git(repo, "remote", "add", "origin", str(remote))
    git(
        repo,
        "push",
        "-q",
        "origin",
        f"{old}:refs/heads/feature/rebase",
        f"{base}:refs/heads/main",
    )
    # Publish the contender's object without advancing the tested PR ref yet.
    contender = commit(repo, "concurrent.txt", "other actor\n")
    git(repo, "push", "-q", "origin", f"{contender}:refs/heads/contender")
    pushes = []

    class Runner:
        def run(self, args, *, check=True, **kwargs):
            if args[:2] == ["git", "push"]:
                pushes.append(args)
                if failure == "concurrent":
                    git(
                        remote,
                        "update-ref",
                        "refs/heads/feature/rebase",
                        contender,
                        old,
                    )
            return subprocess.run(
                args, cwd=repo, check=check, capture_output=True, text=True
            )

    evidence = {}
    if failure:
        message = (
            "not an ancestor"
            if failure == "equivalence"
            else "changed during publication"
        )
        with pytest.raises(pr_publish.PublicationError, match=message):
            pr_publish.converge_remote_publication_head(
                Runner(),
                head="feature/rebase",
                expected_head=expected,
                repo=repo,
                base_head=base,
                equivalence_evidence=evidence,
            )
        assert evidence == {}
        assert git(remote, "rev-parse", "refs/heads/feature/rebase") == (
            contender if failure == "concurrent" else old
        )
    else:
        assert (
            pr_publish.converge_remote_publication_head(
                Runner(),
                head="feature/rebase",
                expected_head=expected,
                repo=repo,
                base_head=base,
                equivalence_evidence=evidence,
            )
            == expected
        )
        assert evidence["method"] == "complete-author-replay"
        assert git(remote, "rev-parse", "refs/heads/feature/rebase") == expected
    assert git(remote, "rev-parse", "refs/heads/main") == base
    assert len(pushes) == (0 if failure == "equivalence" else 1)
    for push in pushes:
        assert f"--force-with-lease=refs/heads/feature/rebase:{old}" in push
        assert "--force" not in push


@pytest.mark.parametrize(
    "change,expected",
    [
        (None, True),
        ("author", False),
        ("foreign", False),
        ("security-overlap", False),
        ("semantic-overlap", False),
        ("neutral-overlap", True),
        ("security", False),
        ("missing-section", False),
        ("missing-verdict", False),
    ],
)
@pytest.mark.parametrize("recorded_carry", [False, True])
def test_sectioned_review_rebase_coverage(history, change, expected, recorded_carry):
    from loopzero.kernel import patch_identity
    from loopzero.review import _tree_coverage as coverage

    repo, _, _, old_base = history
    git(repo, "checkout", "-qb", "security-base", old_base)
    original = "".join(f"setting_{i} = {i}\n" for i in range(30))
    candidate_path = "component.py" if change == "semantic-overlap" else "config.py"
    if change == "semantic-overlap":
        original = original.replace("setting_0", "auth_required")
    old_base = commit(repo, candidate_path, original)
    if change == "neutral-overlap":
        old_base = commit(repo, "author.txt", original)
    git(repo, "checkout", "-qb", "security-reviewed")
    old = commit(repo, candidate_path, original.replace(" = 0\n", " = 100\n"))
    if change == "neutral-overlap":
        old = commit(repo, "author.txt", original.replace(" = 0\n", " = 100\n"))
    git(repo, "checkout", "-q", "security-base")
    if change == "neutral-overlap":
        commit(
            repo, "author.txt", original.replace("setting_29 = 29", "setting_29 = 200")
        )
    if change in {"security-overlap", "semantic-overlap"}:
        commit(
            repo,
            candidate_path,
            original.replace("setting_29 = 29", "setting_29 = 200"),
        )
    base = commit(repo, "upstream.txt", "docs-only base motion\n")
    git(repo, "checkout", "-B", "rebased")
    git(repo, "cherry-pick", f"{old_base}..{old}")
    if change == "security":
        git(repo, "checkout", "-q", "security-base")
        base = commit(repo, "middleware.py", "credential_mode = 'changed'\n")
        git(repo, "checkout", "-q", "rebased")
        git(repo, "rebase", "security-base")
    elif change == "author":
        commit(repo, "author.txt", "different author content\n")
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    source = {
        "version": 2,
        "ref": "refs/heads/rebased",
        "head": old,
        "state_sha256": "old-state",
    }
    current = {**source, "head": head, "state_sha256": "current-state"}
    if change == "foreign":
        source = {**source, "ref": "refs/heads/foreign"}
    identity = patch_identity.compute_patch_identity(
        repo, base_sha=old_base, candidate_sha=old
    )
    sections = ["code"] if change == "missing-section" else ["code", "security"]
    terminal = {
        "task_id": "sectioned",
        "source_identity": source,
        "snapshot_sha": old,
        "snapshot_tree_sha": identity["candidate_tree_sha"],
        "patch_identity": identity,
        "review_intent": "delivery-code-review",
        "task_contract": {"review_intent": "delivery-code-review"},
        "review_chain_receipt": {
            "required_sections": sections,
            "sections": {
                s: {"completion": "completed", "verdict": "clean"} for s in sections
            },
        },
    }
    records = [terminal]
    if recorded_carry:
        carried = patch_identity.prove_commit_carry(repo, identity, current_head=head)
        if carried:
            records.append(
                patch_identity.build_patch_carry_record(
                    terminal,
                    current_source=current,
                    current_identity=carried[0],
                    replay_receipt=carried[1],
                )
            )
    verdicts = {} if change == "missing-verdict" else {"sectioned": "pass"}
    assert (
        coverage.review_task_covers_tree(
            repo,
            records=records,
            accepted_terminals={"sectioned": terminal},
            latest_verdicts=verdicts,
            finding_records=[],
            task_id="sectioned",
            lens="security",
            current_source=current,
            current_tree=tree,
        )
        is expected
    )
    if change is None:
        assert coverage.review_task_covers_tree(
            repo,
            records=records,
            accepted_terminals={"sectioned": terminal},
            latest_verdicts=verdicts,
            finding_records=[],
            task_id="sectioned",
            lens="code",
            current_source=current,
            current_tree=tree,
        )


@pytest.mark.parametrize(
    "change,expected",
    [
        (None, True),
        ("author", False),
        ("security", False),
        ("missing-section", False),
        ("missing-verdict", False),
        ("foreign", False),
        ("unbound-snapshot", False),
        ("missing-repair-base", False),
        ("mixed-security-repair", False),
    ],
)
@pytest.mark.skip(reason="(a) consumer publication CLI composition stays outside loopzero")
def test_sectioned_delta_repair_then_equivalent_rebase(
    history, change, expected, monkeypatch
):
    from loopzero.kernel import patch_identity
    from loopzero.review import _tree_coverage as coverage

    repo, _, _, base = history
    git(repo, "checkout", "-B", "repaired", base)
    full = commit(repo, "config.py", "limit = 1\n")
    delta = commit(repo, "config.py", "limit = 2\n")
    repaired = commit(repo, "author.txt", "deterministic repair\n")
    if change == "mixed-security-repair":
        repaired = commit(repo, "config.py", "limit = 3\n")
    repair_tree = git(repo, "rev-parse", "HEAD^{tree}")
    git(repo, "checkout", "-B", "new-base", base)
    new_base = commit(repo, "upstream.txt", "neutral base motion\n")
    if change == "security":
        new_base = commit(repo, "middleware.py", "auth_required = False\n")
    git(repo, "checkout", "-q", "repaired")
    git(repo, "rebase", "new-base")
    if change == "author":
        commit(repo, "author.txt", "unreviewed author drift\n")
    git(repo, "update-ref", "refs/remotes/origin/main", new_base)
    head = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    sections = ["code"] if change == "missing-section" else ["code", "security"]

    def terminal(task, sha):
        identity = patch_identity.compute_patch_identity(
            repo, base_sha=base, candidate_sha=sha
        )
        return {
            "task_id": task,
            "source_identity": {
                "version": 2,
                "ref": "refs/heads/repaired",
                "head": sha,
                "state_sha256": sha,
            },
            "snapshot_sha": sha,
            "snapshot_tree_sha": identity["candidate_tree_sha"],
            "patch_identity": identity,
            "review_intent": "delivery-code-review",
            "task_contract": {"review_intent": "delivery-code-review"},
            "review_chain_receipt": {
                "schema_version": "ReviewChainReceiptV1",
                "required_sections": sections,
                "sections": {
                    s: {
                        "completion": "completed",
                        "verdict": "clean",
                        "finding_ids": [s + "-finding"],
                    }
                    for s in sections
                },
            },
        }

    terminals = {"full": terminal("full", full), "delta": terminal("delta", delta)}
    terminals["delta"]["delta_from_tree_sha"] = terminals["full"]["snapshot_tree_sha"]
    findings = [
        {
            "finding_id": s + "-finding",
            "review_task_id": "delta",
            "state": "addressed",
            "snapshot_tree_sha": terminals["delta"]["snapshot_tree_sha"],
            "deterministic_evidence": {
                "target_tree_sha": repair_tree,
                "target_snapshot_sha": full
                if change == "unbound-snapshot"
                else repaired,
            },
        }
        for s in ("code", "security")
    ]
    if change == "missing-repair-base":
        terminals["delta"]["patch_identity"].pop("base_sha")
    verdicts = {"full": "pass", "delta": "pass"}
    if change == "missing-verdict":
        verdicts.pop("delta")
    current = {
        **terminals["delta"]["source_identity"],
        "head": head,
        "state_sha256": head,
    }
    if change == "foreign":
        terminals["delta"]["source_identity"]["ref"] = "refs/heads/foreign"

    def covered(candidate, candidate_tree):
        return coverage.review_task_covers_tree(
            repo,
            records=list(terminals.values()),
            accepted_terminals=terminals,
            latest_verdicts=verdicts,
            finding_records=findings,
            task_id="delta",
            lens="security",
            current_source=candidate,
            current_tree=candidate_tree,
        )

    if change is None:
        assert covered({**current, "head": repaired}, repair_tree)
        # The model-reviewed guard differs from the accepted deterministic repair.
        assert (
            coverage.section_patch_equivalence(
                repo,
                terminals["delta"]["patch_identity"],
                patch_identity.compute_patch_identity(
                    repo, base_sha=new_base, candidate_sha=head
                ),
                lens="security",
            )
            is None
        )
    assert covered(current, tree) is expected

    if change in {None, "security", "author", "unbound-snapshot", "foreign"}:
        publish_candidate = _publication_harness(
            repo, monkeypatch, terminals, findings, verdicts
        )
        if change is None:
            git(repo, "reset", "--hard", repaired)
            publish_candidate(base, repaired)
            git(repo, "reset", "--hard", head)
            publish_candidate(new_base, head)
        else:
            with pytest.raises(SystemExit, match="2"):
                publish_candidate(new_base, head)
            assert not (repo / ".audit/pr-publications").exists()


def _publication_harness(repo, monkeypatch, terminals, findings, verdicts):
    """Real Git/risk/coverage/publication; isolate host authority and GitHub IO."""
    import json
    from contextlib import nullcontext

    import delivery_review_risk
    from loopzero.delivery import publish as publisher

    run_id = "sr_" + "a" * 32
    body = (
        "## Context and goal\n\n"
        "- **Context:** Equivalent rebases carry accepted review evidence.\n"
        "- **Problem:** Repair closure did not compose with rebase coverage.\n"
        "- **Goal:** Publish the covered candidate with exact-head risk.\n\n"
        "Closes #4160\n\n## Validation\n\n- Real Git regression.\n\n"
        f"<!-- skill-run-id: {run_id} -->\n"
    )
    audit = repo / ".audit"
    audit.mkdir()
    run_history = audit / "skill-runs"
    run_history.mkdir()
    # This equivalence scenario models an existing pre-cutover delivery.
    (run_history / "2026-01-01.jsonl").write_text(
        json.dumps({"run_id": run_id, "event": "start"}) + "\n"
    )
    # Keep evidence untracked and ignored, as in a delivery worktree.
    (repo / ".git/info/exclude").write_text(".audit/\n")
    body_file = audit / "body.md"
    body_file.write_text(body)

    class GitRunner:
        git_dir = repo / ".git"

        def run(self, args, *, check=True, preserve_output_bytes=False, **kwargs):
            result = subprocess.run(args, cwd=repo, capture_output=True, check=check)
            return subprocess.CompletedProcess(
                args, result.returncode, result.stdout.decode(), result.stderr.decode()
            )

    class ApiRunner:
        def __init__(self, command_runner):
            self.command_runner = command_runner

        def run_json(self, args, *, payload=None):
            if args[-1] == "repos/{owner}/{repo}":
                return {"owner": {"login": "owner"}, "full_name": "owner/repo"}
            if args[-1] == "repos/{owner}/{repo}/pulls" and "GET" in args:
                return []
            assert args[-1] in {
                "repos/{owner}/{repo}/pulls",
                "repos/{owner}/{repo}/pulls/4160",
            }
            return {
                "number": 4160,
                "html_url": "https://example.test/pull/4160",
                "head": {
                    "ref": "repaired",
                    "sha": git(repo, "rev-parse", "HEAD"),
                    "repo": {"full_name": "owner/repo"},
                },
                "base": {"ref": "main"},
                "body": body,
            }

    monkeypatch.chdir(repo)
    monkeypatch.setattr(publisher, "SecureGitRunner", lambda *a, **kw: GitRunner())
    monkeypatch.setattr(publisher, "SubprocessRunner", ApiRunner)
    monkeypatch.setattr(publisher, "_repo_root", lambda *a: repo)
    monkeypatch.setattr(
        publisher, "require_canonical_publisher_source", lambda *a: None
    )
    monkeypatch.setattr(publisher, "require_active_publication_run", lambda **kw: None)
    monkeypatch.setattr(publisher, "worktree_lease", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(
        publisher, "_all_dispatch_records", lambda *a: list(terminals.values())
    )
    monkeypatch.setattr(publisher, "dispatch_lifecycle_violations", lambda *a, **kw: [])
    monkeypatch.setattr(publisher, "accepted_review_terminals", lambda *a: terminals)
    # Patch the namespace the publisher's bound function actually reads: sibling
    # suites re-register ``pr_publish`` in ``sys.modules``, so a fresh
    # ``import pr_publish_risk`` here can resolve to a different module object
    # than the one ``review_risk_envelope_for_publication`` closes over.
    monkeypatch.setitem(
        publisher.review_risk_envelope_for_publication.__globals__,
        "accepted_review_terminals",
        lambda *a: terminals,
    )
    monkeypatch.setattr(
        publisher,
        "latest_accepted_review_terminal",
        lambda records, task: terminals[task],
    )
    monkeypatch.setattr(
        publisher,
        "authenticated_verdicts",
        lambda *a, **kw: {t: {"verdict": v} for t, v in verdicts.items()},
    )
    monkeypatch.setattr(publisher, "authenticated_supersessions", lambda *a: [])
    monkeypatch.setattr(publisher, "load_finding_records", lambda *a, **kw: findings)
    monkeypatch.setattr(publisher, "open_important_finding_ids", lambda *a, **kw: ())
    monkeypatch.setattr(
        publisher, "validate_body_against_trusted_base", lambda *a, **kw: None
    )
    monkeypatch.setattr(publisher, "require_obligation_acknowledgment", lambda *a: None)
    monkeypatch.setattr(
        publisher,
        "converge_remote_publication_head",
        lambda *a, **kw: kw["expected_head"],
    )
    monkeypatch.setattr(
        publisher,
        "require_remote_publication_prerequisites",
        lambda *a, **kw: {"sha": kw["expected_head"]},
    )

    def publish(base, head):
        monkeypatch.setattr(
            publisher, "trusted_publication_base_head", lambda *a, **kw: base
        )
        assert (
            publisher.main(
                [
                    "--title",
                    "fix: carry accepted security repair",
                    "--body-file",
                    str(body_file),
                    "--head",
                    "repaired",
                    "--expected-head",
                    head,
                    "--review-task-id",
                    "delta",
                    "--origin-url",
                    "https://github.com/owner/repo.git",
                    "--git-config-sha256",
                    "d" * 64,
                    "--authority-repo",
                    str(repo),
                ]
            )
            == 0
        )
        verified = delivery_review_risk.verify_publication_review_risk(
            repo,
            audit / "pr-publications",
            pr=4160,
            run_id=run_id,
            expected_head=head,
            expected_base=base,
            runner=GitRunner(),
        )
        assert verified["effective_tier"] == "T2"
        assert verified["artifact"]["head_tree_sha"] == git(
            repo, "rev-parse", "HEAD^{tree}"
        )
        assert verified["review_task_id"] == "delta"
        rows = [
            json.loads(line)
            for path in (audit / "pr-publications").glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        assert sum(r["expected_head"] == head for r in rows) == 1
        with pytest.raises(
            delivery_review_risk.ReviewRiskError,
            match="missing for the PR/run/exact head",
        ):
            delivery_review_risk.verify_publication_review_risk(
                repo,
                audit / "pr-publications",
                pr=4160,
                run_id=run_id,
                expected_head="f" * 40,
                runner=GitRunner(),
            )

    return publish


@pytest.mark.parametrize("unrelated", ["foreign", "missing-verdict", "code-only"])
def test_repair_rebase_cannot_borrow_unrelated_snapshot_base(history, unrelated):
    from loopzero.kernel import patch_identity
    from loopzero.review import _tree_coverage as coverage

    repo, _, _, base = history
    git(repo, "checkout", "-B", "reviewed", base)
    reviewed = commit(repo, "config.py", "limit = 2\n")
    repaired = commit(repo, "author.txt", "repair\n")
    repair_tree = git(repo, "rev-parse", "HEAD^{tree}")
    git(repo, "checkout", "-B", "security-base", base)
    upstream = commit(repo, "config.py", "limit = 2\n")
    # Keep identical bytes but distinct history from the reviewed author commit.
    git(repo, "commit", "--amend", "-qm", "upstream security change")
    upstream = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "unrelated")
    snapshot = commit(repo, "author.txt", "repair\n")
    assert git(repo, "rev-parse", "HEAD^{tree}") == repair_tree
    git(repo, "checkout", "-q", "security-base")
    new_base = commit(repo, "upstream.txt", "neutral addition\n")
    git(repo, "checkout", "-B", "reviewed")
    head = commit(repo, "author.txt", "repair\n")
    git(repo, "update-ref", "refs/remotes/origin/main", new_base)
    tree = git(repo, "rev-parse", "HEAD^{tree}")

    def record(candidate, candidate_base, sections):
        identity = patch_identity.compute_patch_identity(
            repo, base_sha=candidate_base, candidate_sha=candidate
        )
        return {
            "source_identity": {
                "version": 2,
                "ref": "refs/heads/reviewed",
                "head": candidate,
                "state_sha256": candidate,
            },
            "snapshot_sha": candidate,
            "snapshot_tree_sha": identity["candidate_tree_sha"],
            "patch_identity": identity,
            "review_intent": "delivery-code-review",
            "task_contract": {"review_intent": "delivery-code-review"},
            "review_chain_receipt": {
                "schema_version": "ReviewChainReceiptV1",
                "required_sections": sections,
                "sections": {s: {"finding_ids": [s]} for s in sections},
            },
        }

    terminal = record(reviewed, base, ["code", "security"])
    other = record(
        snapshot,
        upstream,
        ["code"] if unrelated == "code-only" else ["code", "security"],
    )
    if unrelated == "foreign":
        other["source_identity"]["ref"] = "refs/heads/foreign"
    findings = [
        {
            "finding_id": "security",
            "state": "addressed",
            "review_task_id": "reviewed",
            "snapshot_tree_sha": terminal["snapshot_tree_sha"],
            "deterministic_evidence": {
                "target_snapshot_sha": repaired,
                "target_tree_sha": repair_tree,
            },
        }
    ]
    verdicts = {"reviewed": "pass"}
    if unrelated != "missing-verdict":
        verdicts["other"] = "pass"

    def covered(terminals):
        return coverage.review_task_covers_tree(
            repo,
            records=list(terminals.values()),
            accepted_terminals=terminals,
            latest_verdicts=verdicts,
            finding_records=findings,
            task_id="reviewed",
            lens="security",
            current_source={
                **terminal["source_identity"],
                "head": head,
                "state_sha256": head,
            },
            current_tree=tree,
        )

    assert not covered({"reviewed": terminal})
    assert not covered({"reviewed": terminal, "other": other})
