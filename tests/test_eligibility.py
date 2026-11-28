"""Review evidence counts only when every binding names the exact head under review."""

from contextlib import nullcontext

import pytest

from loopzero import cli, eligibility, github, runners
from tests.conftest import git, review_summary
from tests.test_runners import _native_codex_result

HEAD, OTHER = "a" * 40, "c" * 40
REPO, NUMBER, PUBLISHERS = "acme/widgets", 7, ("bot",)


def codex_review(*, marker=None, evidence_head=HEAD, commit_id=HEAD):
    """A published Codex review at HEAD; each keyword swaps one binding for another head."""
    published = github._review_body(_native_codex_result(HEAD), [], cli.review_marker(HEAD, "primary"),
                                     publication=(REPO, NUMBER))
    lines = published.splitlines()
    if marker:
        lines[0] = marker
    if evidence_head != HEAD:
        lines[1] = runners.encode_codex_evidence(runners.codex_evidence(_native_codex_result(evidence_head)))
    return {"id": 1, "commit_id": commit_id, "user": {"login": "bot"}, "state": "COMMENTED",
            "body": "\n".join(lines)}


@pytest.mark.parametrize("binding", [
    {"marker": cli.review_marker(OTHER, "primary")},
    {"marker": cli.review_marker(HEAD, "delta")},
    {"evidence_head": OTHER},
    {"commit_id": OTHER},
])
def test_codex_evidence_projection_binds_marker_kind_evidence_and_commit(binding):
    present, evidence = eligibility.review_projection(codex_review(), HEAD, REPO, NUMBER)
    assert present and evidence["head"] == HEAD and evidence["complete"]
    assert eligibility.review_projection(codex_review(**binding), HEAD, REPO, NUMBER) == (True, None)


def test_lone_codex_evidence_line_is_present_but_never_evidence():
    lone = {**codex_review(), "body": codex_review()["body"].splitlines()[1]}
    assert eligibility.review_projection(lone, HEAD, REPO, NUMBER) == (True, None)


def legacy_review(commit_id, reviewed=HEAD):
    return {"id": 2, "commit_id": commit_id, "user": {"login": "bot"}, "state": "APPROVED",
            "body": f"{cli.review_marker(reviewed, 'primary')}\n{review_summary(reviewed)}"}


def test_review_counts_only_when_github_recorded_it_on_the_head():
    current = legacy_review(HEAD)
    discussion = current | {"state": "COMMENTED", "body": "What does `loopzero:review` mean?"}
    assert eligibility.readiness_review([current, discussion], HEAD, PUBLISHERS, repo=REPO, number=NUMBER, base_branch="main") is current
    assert eligibility.eligible_review([legacy_review(OTHER)], HEAD, PUBLISHERS) is None


@pytest.mark.parametrize("line", [
    "Prose quotes **approve**.",
    "    " + review_summary(HEAD),
    "loopzero primary review by somebody: **approve** (example)",
    review_summary(OTHER),
])
def test_comment_needs_a_complete_summary_for_eligibility_and_review_budget(line):
    review = legacy_review(HEAD) | {"state": "COMMENTED", "body": f"{cli.review_marker(HEAD, 'primary')}\n{line}"}
    assert eligibility.eligible_review([review], HEAD, PUBLISHERS) is None
    assert cli._pr_markers([review], HEAD, PUBLISHERS, repo=REPO, number=NUMBER) == []


def test_ancestor_review_must_itself_be_eligible_at_its_commit(monkeypatch):
    monkeypatch.setattr(eligibility, "base_merge_material", lambda *_, **__: b"")
    readiness = dict(repo=REPO, number=NUMBER, base_branch="main")
    reviewed = "b" * 40
    covering = legacy_review(reviewed, reviewed)
    assert eligibility.readiness_review([covering], HEAD, PUBLISHERS, **readiness) is covering
    misplaced = legacy_review(OTHER, reviewed)
    assert eligibility.readiness_review([misplaced], HEAD, PUBLISHERS, **readiness) is None


@pytest.fixture
def base_merge(git_repo, monkeypatch):
    def build(*, unrelated_root=False, pr_head=None, pr_base_ref="main", pr_base_sha=None):
        git(git_repo, "switch", "-qc", "source")
        git(git_repo, "commit", "-qm", "reviewed source", "--allow-empty")
        reviewed = git(git_repo, "rev-parse", "HEAD").strip()
        git(git_repo, "switch", "-q", "main")
        if unrelated_root:
            git(git_repo, "switch", "-q", "--orphan", "imported")
            git(git_repo, "commit", "-qm", "imported history", "--allow-empty")
            git(git_repo, "switch", "-q", "main")
            git(git_repo, "merge", "-q", "--no-edit", "--allow-unrelated-histories", "imported")
        else:
            git(git_repo, "commit", "-qm", "base advance", "--allow-empty")
        base = git(git_repo, "rev-parse", "HEAD").strip()
        git(git_repo, "switch", "-q", "source")
        git(git_repo, "merge", "-q", "--no-edit", "main")
        head = git(git_repo, "rev-parse", "HEAD").strip()
        pr = {"head": {"sha": pr_head or head}, "base": {"ref": pr_base_ref, "sha": pr_base_sha or base}}
        monkeypatch.setattr(github, "api_get", lambda path: pr)
        monkeypatch.setattr(eligibility.candidate, "fetched_git",
                            lambda *_: nullcontext(lambda *args, **_: git(git_repo, *args)))
        return eligibility.base_merge_material(REPO, NUMBER, reviewed, head, "main", clean=True)
    return build


@pytest.mark.parametrize("change", [
    {"unrelated_root": True}, {"pr_head": OTHER}, {"pr_base_ref": "release"},
    {"pr_base_sha": "main"},
])
def test_clean_base_merge_needs_live_head_base_and_complete_history(base_merge, change):
    assert base_merge(**change) is None


def test_clean_base_merge_is_proven_inheritance(base_merge):
    assert base_merge() == b""
