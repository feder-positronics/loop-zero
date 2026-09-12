import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_module(name: str = "delivery_review_risk"):
    if name == "pr_publish_risk":
        from loopzero.delivery import publish

        return publish
    assert name == "delivery_review_risk"
    from loopzero.review import risk

    return risk


module = load_module()


@pytest.fixture(autouse=True)
def configured_target_risk_policy():
    from loopzero.review import _ci_path_classifier, _security_scope

    previous = (
        module._SETTINGS.get(),
        _ci_path_classifier.PATH_CLASSES,
        _ci_path_classifier.PARENT_CLASSES,
        _security_scope._ALWAYS_SECURITY_REVIEW_PATTERNS,
        _security_scope._REQUIRED_SECTIONS,
    )
    module.configure(
        SimpleNamespace(
            path_classes={
                "docs": ("docs/**",),
                "backend": ("fastapi_backend/**",),
                "frontend": ("nextjs-frontend/**",),
                "tooling": ("scripts/**",),
            },
            path_class_parents={},
            security_patterns=(
                "**/*.sh", "**/*.bash", "**/*.zsh", "**/*.ps1",
                "**/config.py", "**/auth/**", "**/middleware/**",
                "**/integrations/**", "pyproject.toml", "package.json",
            ),
            required_sections=("code",),
        )
    )
    try:
        yield
    finally:
        module._SETTINGS.set(previous[0])
        _ci_path_classifier.PATH_CLASSES = previous[1]
        _ci_path_classifier.PARENT_CLASSES = previous[2]
        _security_scope._ALWAYS_SECURITY_REVIEW_PATTERNS = previous[3]
        _security_scope._REQUIRED_SECTIONS = previous[4]

from loopzero.kernel import seams

seams.configure(delivery_controller_records=lambda rows: rows)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.fixture
def risk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "risk@example.test"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Risk Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def commit_all(repo: Path, message: str = "candidate") -> tuple[str, str]:
    base = git(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)
    return base, git(repo, "rev-parse", "HEAD")


def test_docs_only_regular_blobs_are_the_only_t0(risk_repo: Path) -> None:
    path = risk_repo / "docs" / "guide.md"
    path.parent.mkdir()
    path.write_text("safe prose\n", encoding="utf-8")
    base, head = commit_all(risk_repo)

    envelope = module.compute_review_risk(risk_repo, base, head)
    artifact = module.verify_review_risk(
        risk_repo,
        envelope["review_risk_json"],
        envelope["review_risk_sha256"],
    )["artifact"]

    assert artifact["tier"] == "T0"
    assert artifact["reasons"] == ["docs-only"]
    assert artifact["changed_paths"] == ["docs/guide.md"]
    assert artifact["required_sections"] == []


def test_executable_docs_blob_fails_closed_to_review(risk_repo: Path) -> None:
    path = risk_repo / "docs" / "run.md"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    base, head = commit_all(risk_repo)

    artifact = module.parse_review_risk(
        module.compute_review_risk(risk_repo, base, head)["review_risk_json"]
    )

    assert artifact["tier"] == "T1"
    assert "special-git-mode" in artifact["reasons"]


def test_all_executable_test_code_remains_reviewed(risk_repo: Path) -> None:
    path = risk_repo / "fastapi_backend" / "tests" / "unit" / "test_new.py"
    path.parent.mkdir(parents=True)
    path.write_text("def test_new(): assert True\n", encoding="utf-8")
    base, head = commit_all(risk_repo)

    artifact = module.parse_review_risk(
        module.compute_review_risk(risk_repo, base, head)["review_risk_json"]
    )

    assert artifact["tier"] == "T1"
    assert artifact["required_sections"] == ["code"]
    assert "review-required" in artifact["reasons"]


def test_security_trigger_is_t2_and_requires_security_section(risk_repo: Path) -> None:
    path = risk_repo / "fastapi_backend" / "app" / "config.py"
    path.parent.mkdir(parents=True)
    path.write_text("API_TOKEN = 'placeholder'\n", encoding="utf-8")
    base, head = commit_all(risk_repo)

    artifact = module.parse_review_risk(
        module.compute_review_risk(risk_repo, base, head)["review_risk_json"]
    )

    assert artifact["tier"] == "T2"
    assert artifact["security_trigger_paths"] == ["fastapi_backend/app/config.py"]
    assert artifact["required_sections"] == ["code", "security"]


@pytest.mark.parametrize("suffix", [".sh", ".bash", ".zsh", ".ps1"])
@pytest.mark.parametrize("directory", ["scripts", "tests"])
def test_tracked_shell_edit_requires_security_in_verified_classification(
    risk_repo: Path, suffix: str, directory: str
) -> None:
    path = risk_repo / directory / f"hook{suffix}"
    path.parent.mkdir()
    path.write_text("echo ready\n", encoding="utf-8")
    commit_all(risk_repo, "existing hook")
    path.write_text("curl https://example.test/tool\n", encoding="utf-8")
    base, head = commit_all(risk_repo, "change hook")

    envelope = module.compute_review_risk(risk_repo, base, head)
    verified = module.verify_review_risk(
        risk_repo, envelope["review_risk_json"], envelope["review_risk_sha256"]
    )

    assert verified["effective_tier"] == "T2"
    assert verified["effective_required_sections"] == ["code", "security"]
    assert verified["effective_security_trigger_paths"] == [f"{directory}/hook{suffix}"]


def test_rename_binds_both_sides_and_cannot_hide_code_destination(
    risk_repo: Path,
) -> None:
    source = risk_repo / "docs" / "old.md"
    source.parent.mkdir()
    source.write_text("prose\n", encoding="utf-8")
    commit_all(risk_repo, "source")
    target = risk_repo / "scripts" / "old.py"
    target.parent.mkdir()
    subprocess.run(["git", "mv", str(source), str(target)], cwd=risk_repo, check=True)
    base, head = commit_all(risk_repo, "rename")

    artifact = module.parse_review_risk(
        module.compute_review_risk(risk_repo, base, head)["review_risk_json"]
    )

    assert artifact["tier"] == "T1"
    assert artifact["changed_paths"] == ["docs/old.md", "scripts/old.py"]


def test_noncanonical_or_conflicting_artifact_is_rejected(risk_repo: Path) -> None:
    path = risk_repo / "docs" / "guide.md"
    path.parent.mkdir()
    path.write_text("safe prose\n", encoding="utf-8")
    base, head = commit_all(risk_repo)
    envelope = module.compute_review_risk(risk_repo, base, head)
    noncanonical = json.dumps(json.loads(envelope["review_risk_json"]), indent=2)

    with pytest.raises(module.ReviewRiskError, match="canonical"):
        module.verify_review_risk(
            risk_repo, noncanonical, envelope["review_risk_sha256"]
        )
    with pytest.raises(module.ReviewRiskError, match="digest"):
        module.verify_review_risk(risk_repo, envelope["review_risk_json"], "0" * 64)


def test_diff_digest_preserves_non_utf8_git_bytes(risk_repo: Path) -> None:
    path = risk_repo / "docs" / "guide.md"
    path.parent.mkdir()
    path.write_bytes(b"candidate-\xff\n")
    base, head = commit_all(risk_repo)

    envelope = module.compute_review_risk(risk_repo, base, head)
    artifact = module.parse_review_risk(envelope["review_risk_json"])
    raw_diff = subprocess.check_output(
        [
            "git",
            "diff",
            "--binary",
            "--full-index",
            "--no-renames",
            "--no-ext-diff",
            base,
            head,
            "--",
        ],
        cwd=risk_repo,
    )

    assert artifact["diff_sha256"] == hashlib.sha256(raw_diff).hexdigest()


def require_historical_commits(root: Path, commit_shas: list[str]) -> None:
    missing_shas = [
        sha
        for sha in commit_shas
        if subprocess.run(
            ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        != 0
    ]
    if not missing_shas:
        return
    shallow = subprocess.check_output(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=root,
        text=True,
    ).strip()
    if shallow == "true":
        pytest.skip(
            "historical replay corpus requires full Git history; missing commits: "
            + ", ".join(missing_shas)
        )
    pytest.fail(
        "historical replay corpus references missing commits in a full clone: "
        + ", ".join(missing_shas)
    )


def test_missing_historical_commit_skips_in_shallow_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1),
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: "true\n")

    with pytest.raises(pytest.skip.Exception, match="requires full Git history"):
        require_historical_commits(tmp_path, ["a" * 40])


def test_missing_historical_commit_fails_in_full_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1),
    )
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: "false\n")

    with pytest.raises(pytest.fail.Exception, match="missing commits in a full clone"):
        require_historical_commits(tmp_path, ["b" * 40])


def test_historical_changed_path_replay_corpus_has_no_t2_escape(
    tmp_path: Path,
) -> None:
    corpus_path = Path(__file__).parent / "fixtures" / "delivery_review_risk_replay_v1.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))

    repo = tmp_path / "historical-replay"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "risk-replay@example.test"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Risk Replay"], cwd=repo, check=True
    )
    (repo / "README.md").write_text("historical replay\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "replay root"], cwd=repo, check=True)

    for index, case in enumerate(corpus["cases"]):
        for relative in case["changed_paths"]:
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# preserved historical baseline {index}\n",
                encoding="utf-8",
            )
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", f"{case['name']} base"], cwd=repo, check=True
        )
        base_sha = git(repo, "rev-parse", "HEAD")

        for relative in case["changed_paths"]:
            path = repo / relative
            historical_change = (
                "def verify_authority():\n    raise RuntimeError('provider timeout')\n"
                if relative
                in {
                    "scripts/util/dispatch_authority.py",
                    "scripts/util/dispatch_authority_projection.py",
                }
                else f"# preserved historical change {index}\n"
            )
            path.write_text(historical_change, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", f"{case['name']} head"], cwd=repo, check=True
        )
        head_sha = git(repo, "rev-parse", "HEAD")

        envelope = module.compute_review_risk(repo, base_sha, head_sha)
        artifact = module.parse_review_risk(envelope["review_risk_json"])
        assert artifact["changed_paths"] == sorted(case["changed_paths"]), case["name"]
        assert artifact["tier"] == case["expected_tier"], case["name"]
        assert artifact["security_trigger_paths"] == case["security_trigger_paths"]
        assert not (
            case["expected_tier"] == "T2" and artifact["tier"] == "T0"
        )


def test_strictest_tier_is_monotonic() -> None:
    assert module.strictest_tier("T0", "T1") == "T1"
    assert module.strictest_tier("T2", "T0") == "T2"


def test_publication_verifier_rejects_non_oid_artifact_refs_before_git(
    risk_repo: Path,
) -> None:
    path = risk_repo / "docs" / "guide.md"
    path.parent.mkdir()
    path.write_text("safe prose\n", encoding="utf-8")
    base, head = commit_all(risk_repo)
    artifact = module.parse_review_risk(
        module.compute_review_risk(risk_repo, base, head)["review_risk_json"]
    )
    artifact["head_sha"] = "--help"
    payload = module.canonical_review_risk_json(artifact)

    with pytest.raises(module.ReviewRiskError, match="commit object IDs"):
        module.verify_review_risk(
            risk_repo,
            payload,
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )


def publication_record(
    envelope: dict[str, str], *, pr: int, run_id: str, head: str
) -> dict[str, object]:
    artifact = module.parse_review_risk(envelope["review_risk_json"])
    return {
        "admission_tier_floor": artifact["tier"],
        "expected_head": head,
        "pr": pr,
        "review_exemption": "T0",
        "review_risk_json": envelope["review_risk_json"],
        "review_risk_sha256": envelope["review_risk_sha256"],
        "review_risk_tier": artifact["tier"],
        "review_task_id": None,
        "run_id": run_id,
        "status": "published",
    }


def test_publication_verifier_rederives_exact_head_and_floor(risk_repo: Path) -> None:
    path = risk_repo / "docs" / "guide.md"
    path.parent.mkdir()
    path.write_text("safe prose\n", encoding="utf-8")
    base, head = commit_all(risk_repo)
    envelope = module.compute_review_risk(risk_repo, base, head)
    evidence_dir = risk_repo / ".audit" / "pr-publications"
    evidence_dir.mkdir(parents=True)
    run_id = "sr_" + "a" * 32
    record = publication_record(envelope, pr=42, run_id=run_id, head=head)
    (evidence_dir / "42.jsonl").write_text(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    verified = module.verify_publication_review_risk(
        risk_repo,
        evidence_dir,
        pr=42,
        run_id=run_id,
        expected_head=head,
        authority_records=[],
    )

    assert verified["effective_tier"] == "T0"
    assert verified["artifact"]["head_sha"] == head


def test_publication_adoption_retry_preserves_verifiable_evidence(
    risk_repo: Path,
) -> None:
    publisher = load_module("pr_publish_risk")

    path = risk_repo / "docs" / "guide.md"
    path.parent.mkdir()
    path.write_text("updated prose\n", encoding="utf-8")
    base, head = commit_all(risk_repo)
    envelope = module.compute_review_risk(risk_repo, base, head)
    evidence_dir = risk_repo / ".audit" / "pr-publications"
    run_id = "sr_" + "c" * 32
    record = publication_record(envelope, pr=46, run_id=run_id, head=head)
    original = record | {"adopted": False}
    first_path = publisher.write_published_evidence_once(
        evidence_dir, original, generation=0
    )
    retry_path = publisher.write_published_evidence_once(
        evidence_dir, record | {"adopted": True}, generation=0
    )

    verified = module.verify_publication_review_risk(
        risk_repo,
        evidence_dir,
        pr=46,
        run_id=run_id,
        expected_head=head,
        authority_records=[],
        expected_base=base,
    )

    assert verified["effective_tier"] == "T0"
    assert verified["artifact"]["head_sha"] == head
    assert retry_path == first_path
    rows = [
        json.loads(line)
        for evidence_file in evidence_dir.glob("*.jsonl")
        for line in evidence_file.read_text(encoding="utf-8").splitlines()
    ]
    assert rows == [original]


def test_publication_verifier_rejects_a_different_expected_base(
    risk_repo: Path,
) -> None:
    path = risk_repo / "docs" / "guide.md"
    path.parent.mkdir()
    path.write_text("safe prose\n", encoding="utf-8")
    base, head = commit_all(risk_repo)
    envelope = module.compute_review_risk(risk_repo, base, head)
    evidence_dir = risk_repo / ".audit" / "pr-publications"
    evidence_dir.mkdir(parents=True)
    run_id = "sr_" + "d" * 32
    record = publication_record(envelope, pr=45, run_id=run_id, head=head)
    (evidence_dir / "45.jsonl").write_text(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(module.ReviewRiskError, match="different base"):
        module.verify_publication_review_risk(
            risk_repo,
            evidence_dir,
            pr=45,
            run_id=run_id,
            expected_head=head,
            authority_records=[],
            expected_base="0" * 40,
        )


def test_publication_cli_binds_runner_to_origin_and_git_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def fake_runner(repo, *, origin_url, git_config_sha256):
        return SimpleNamespace(
            origin_url=origin_url, git_config_sha256=git_config_sha256
        )

    def fake_verify(
        repo: Path,
        evidence_dir: Path,
        *,
        pr: int,
        run_id: str,
        expected_head: str,
        expected_base: str,
        runner: object,
        authority_repo: Path,
        **_configuration,
    ) -> dict[str, object]:
        observed.update(
            repo=repo,
            evidence_dir=evidence_dir,
            pr=pr,
            run_id=run_id,
            expected_head=expected_head,
            expected_base=expected_base,
            origin_url=runner.origin_url,
            git_config_sha256=runner.git_config_sha256,
            authority_repo=authority_repo,
        )
        return {"effective_tier": "T1"}

    monkeypatch.setattr(module, "verify_publication_review_risk", fake_verify)
    monkeypatch.setattr(module, "_runner", fake_runner)
    monkeypatch.setattr(
        module,
        "load_profile",
        lambda root: SimpleNamespace(
            path_classes={"docs": ("docs/**",)},
            security_patterns=(),
            required_sections=("code",),
        ),
    )
    run_id = "sr_" + "d" * 32
    digest = "e" * 64

    assert (
        module._main(
            [
                "--repo",
                str(tmp_path),
                "--verify-publication",
                "--evidence-dir",
                str(tmp_path / "evidence"),
                "--pr",
                "42",
                "--run-id",
                run_id,
                "--expected-head",
                "f" * 40,
                "--expected-base",
                "a" * 40,
                "--origin-url",
                "https://github.com/feder-positronics/intelflo.git",
                "--git-config-sha256",
                digest,
            ]
        )
        == 0
    )
    assert observed == {
        "repo": tmp_path,
        "evidence_dir": tmp_path / "evidence",
        "pr": 42,
        "run_id": run_id,
        "expected_head": "f" * 40,
        "expected_base": "a" * 40,
        "origin_url": "https://github.com/feder-positronics/intelflo.git",
        "git_config_sha256": digest,
        "authority_repo": tmp_path,
    }


@pytest.mark.parametrize("defect", ["missing", "duplicate", "weaker-floor"])
def test_publication_verifier_fails_closed_on_evidence_defect(
    risk_repo: Path, defect: str
) -> None:
    path = risk_repo / "docs" / "guide.md"
    path.parent.mkdir()
    path.write_text("safe prose\n", encoding="utf-8")
    base, head = commit_all(risk_repo)
    envelope = module.compute_review_risk(risk_repo, base, head)
    evidence_dir = risk_repo / ".audit" / "pr-publications"
    evidence_dir.mkdir(parents=True)
    run_id = "sr_" + "b" * 32
    if defect != "missing":
        record = publication_record(envelope, pr=43, run_id=run_id, head=head)
        if defect == "weaker-floor":
            record["review_risk_tier"] = "T1"
            record["admission_tier_floor"] = "T0"
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        if defect == "duplicate":
            line += line
        (evidence_dir / "43.jsonl").write_text(line, encoding="utf-8")

    with pytest.raises(module.ReviewRiskError):
        module.verify_publication_review_risk(
            risk_repo,
            evidence_dir,
            pr=43,
                run_id=run_id,
                expected_head=head,
                authority_records=[],
        )


def test_publication_verifier_uses_stricter_tier_after_classifier_change(
    risk_repo: Path, monkeypatch
) -> None:
    path = risk_repo / "docs" / "guide.md"
    path.parent.mkdir()
    path.write_text("safe prose\n", encoding="utf-8")
    base, head = commit_all(risk_repo)
    envelope = module.compute_review_risk(risk_repo, base, head)
    evidence_dir = risk_repo / ".audit" / "pr-publications"
    evidence_dir.mkdir(parents=True)
    run_id = "sr_" + "c" * 32
    record = publication_record(envelope, pr=44, run_id=run_id, head=head)
    record["review_risk_tier"] = "T1"
    record["admission_tier_floor"] = "T1"
    record["review_task_id"] = "review-44"
    record["review_exemption"] = None
    (evidence_dir / "44.jsonl").write_text(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    current = module._artifact(risk_repo, base, head)
    current.update(
        classifier_version="0" * 64,
        tier="T1",
        reasons=["review-required"],
        required_sections=["code"],
    )
    monkeypatch.setattr(module, "_artifact", lambda *_args, **_kwargs: current)

    verified = module.verify_publication_review_risk(
        risk_repo,
        evidence_dir,
        pr=44,
        run_id=run_id,
        expected_head=head,
        authority_records=[],
    )

    assert verified["effective_tier"] == "T1"
    assert verified["artifact"]["classifier_version"] != "0" * 64
