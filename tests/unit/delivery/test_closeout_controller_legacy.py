import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent / "fixtures/closeout_root"
pytestmark = pytest.mark.skip(
    reason="(a) consumer closeout shell composition stays outside loopzero"
)
SCRIPT = (ROOT / "scripts" / "util" / "pr_closeout_impl.sh").read_text(encoding="utf-8")
CLOSEOUT_COMMAND = [
    "/bin/bash",
    "-p",
    str(ROOT / "scripts/util/pr_closeout_impl.sh"),
]
CANONICAL_ORIGIN = "https://github.com/feder-positronics/intelflo.git"
requires_host_job_authority = pytest.mark.skipif(
    os.environ.get("INTELFLO_GUARDIAN_SANDBOX_BOUNDARY") is not None,
    reason="closeout settlement cannot write host job authority from its sandbox",
)


def _git_config_security_digest(config_path: Path) -> str:
    return subprocess.check_output(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts/util/git_config_security.py"),
            str(config_path),
        ],
        text=True,
    ).strip()


def _write_canonical_config(primary: Path) -> Path:
    config_path = primary / ".git" / "config"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        '[remote "origin"]\n' f"\turl = {CANONICAL_ORIGIN}\n",
        encoding="utf-8",
    )
    return config_path


def _reviewed_main_bootstrap_environment(
    primary: Path, delivery: Path
) -> dict[str, str]:
    return {
        "INTELFLO_BOOTSTRAP": "reviewed-main-v1",
        "INTELFLO_TRUSTED_PRIMARY": str(primary),
        "INTELFLO_DELIVERY_ROOT": str(delivery),
        "INTELFLO_TRUSTED_REVISION": "a" * 40,
        "INTELFLO_REMOTE_DEFAULT_REF": "refs/heads/main",
        "INTELFLO_CLOSEOUT_TOOLCHAIN_OID": "b" * 40,
    }


def test_closeout_requires_issue_owned_blueprint_completion_before_merge() -> None:
    merge_marker = "# 1. Merge (optional)"

    assert "--complete-blueprint" not in SCRIPT
    assert "assert_blueprint_completion_before_merge" in SCRIPT
    assert SCRIPT.index("assert_blueprint_completion_before_merge") < SCRIPT.index(
        merge_marker
    )
    assert (
        'git -C "$PRIMARY" ls-tree -r --name-only "$commit_oid" -- docs/design/blueprints'
        in SCRIPT
    )
    assert "docs/design/blueprints/completed/" in SCRIPT
    assert "make docs-blueprint-complete SLUG=$blueprint_slug" in SCRIPT
    assert "--json baseRefOid" not in SCRIPT
    assert 'gh api "repos/{owner}/{repo}/pulls/$PR" --jq .base.sha' in SCRIPT


def test_closeout_preflights_final_workflow_before_final_ci_dispatch() -> None:
    preflight = "--workflow-preflight-only"
    dispatch = '--expected-head "$expected_head"'

    assert preflight in SCRIPT
    assert SCRIPT.index(preflight) < SCRIPT.index(dispatch)


def test_closeout_retains_post_merge_thread_reminder_during_gate_cohort() -> None:
    assert "Resolve open review threads (gh api graphql resolveReviewThread)" in SCRIPT


def test_closeout_requires_canonical_run_id_shape_when_explicit() -> None:
    assert '[[ "$RUN_ID" =~ ^sr_[0-9a-f]{32}$ ]]' in SCRIPT


def test_precommit_checks_each_blueprint_lifecycle_directory_with_one_hook() -> None:
    config_text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(config_text)
    hook = ROOT / "scripts" / "hooks" / "blueprint-frontmatter-check.sh"
    hook_config = next(
        item
        for repo in config["repos"]
        for item in repo["hooks"]
        if item.get("id") == "blueprint-frontmatter-check"
    )
    selector = re.compile(hook_config["files"])

    assert "gated-frontmatter-check" not in config_text
    assert hook_config["entry"] == "scripts/hooks/blueprint-frontmatter-check.sh"
    for lifecycle in ("planned", "active", "gated", "completed"):
        assert selector.search(f"docs/design/blueprints/{lifecycle}/fixture.md")
    assert not selector.search("docs/design/blueprints/README.md")
    assert not selector.search("docs/guides/fixture.md")
    assert not selector.search("docs/design/blueprints/active/fixture.txt")
    assert hook.exists()
    assert "blueprint-drift-check.py --offline" in hook.read_text(encoding="utf-8")


def _run_premerge_blueprint_harness(
    tmp_path: Path,
    *,
    completed: bool,
    blueprint_issue: int,
    duplicate_active: bool = False,
) -> subprocess.CompletedProcess[str]:
    primary = tmp_path / "primary"
    worktree = primary / "worktree"
    bin_dir = tmp_path / "bin"
    worktree.mkdir(parents=True)
    bin_dir.mkdir()
    _write_canonical_config(primary)
    base_oid = "a" * 40
    head_oid = "b" * 40
    source = "docs/design/blueprints/active/2026-08-10-sample.md"
    target = "docs/design/blueprints/completed/2026-08-10-sample.md"

    (bin_dir / "git").write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        f'  *"rev-parse --path-format=absolute --git-common-dir"*) echo "{primary}/.git" ;;\n'
        f'  *"rev-parse --show-toplevel"*) echo "{worktree}" ;;\n'
        '  *"rev-parse --verify FETCH_HEAD"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        f'  *"merge-base refs/remotes/origin/main {head_oid}"*) echo "{base_oid}" ;;\n'
        f'  *"cat-file -e {base_oid}^{{commit}}"*|*"cat-file -e {head_oid}^{{commit}}"*) exit 0 ;;\n'
        f'  *"ls-tree -r --name-only {base_oid} -- docs/design/blueprints"*) echo "{source}" ;;\n'
        f'  *"ls-tree -r --name-only {head_oid} -- docs/design/blueprints"*) '
        f'if [ "$CLOSEOUT_COMPLETED" = "1" ]; then echo "{target}"; else echo "{source}"; fi ;;\n'
        f'  *"show {base_oid}:{source}"*|*"show {head_oid}:{target}"*|*"show {head_oid}:{source}"*) '
        'printf "%s\\n" "---" "issue: $CLOSEOUT_BLUEPRINT_ISSUE" "---" ;;\n'
        f'  *"cat-file -e {head_oid}:{target}"*) [ "$CLOSEOUT_COMPLETED" = "1" ] ;;\n'
        f'  *"cat-file -e {head_oid}:{source}"*) [ "$CLOSEOUT_DUPLICATE_ACTIVE" = "1" ] ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "gh").write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '  *"--json baseRefName -q .baseRefName"*) echo "main" ;;\n'
        f'  *"--json headRefOid -q .headRefOid"*) echo "{head_oid}" ;;\n'
        f'  *"api repos/{{owner}}/{{repo}}/pulls/123 --jq .base.sha"*) echo "{base_oid}" ;;\n'
        '  *"--json state -q .state"*) echo "OPEN" ;;\n'
        '  *"pr list --state open --label ci-final --json number"*) echo "[]" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "python3").write_text(
        "#!/bin/bash\n"
        '[ "$1" = "-P" ] && shift\n'
        'case "$1" in\n'
        '  *agent_dispatch.py) echo "{}" ;;\n'
        '  *delivery_review_risk.py) echo \'{"effective_tier":"T1"}\' ;;\n'
        '  *skill_run_log.py) echo "intelflo-v1" ;;\n'
        "  *pr_merge_gate.py) exit 0 ;;\n"
        '  *final_ci_gate.py) echo "merge reached"; exit 42 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "awk").write_text(
        "#!/bin/bash\n"
        "expected_issue=''\n"
        'for argument in "$@"; do\n'
        '  case "$argument" in expected_issue=*) expected_issue=${argument#*=} ;; esac\n'
        "done\n"
        "while IFS= read -r line; do\n"
        '  [ "$line" = "issue: $expected_issue" ] && exit 0\n'
        "done\n"
        "exit 1\n",
        encoding="utf-8",
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    return subprocess.run(
        [
            *CLOSEOUT_COMMAND,
            "--pr",
            "123",
            "--skill",
            "work-issue",
            "--issue",
            "123",
            "--merge",
            "--run-id",
            "sr_" + "a" * 32,
        ],
        cwd=worktree,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CLOSEOUT_BLUEPRINT_ISSUE": str(blueprint_issue),
            "CLOSEOUT_COMPLETED": "1" if completed else "0",
            "CLOSEOUT_DUPLICATE_ACTIVE": "1" if duplicate_active else "0",
            **_reviewed_main_bootstrap_environment(primary, worktree),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_closeout_refuses_before_merge_when_issue_owned_blueprint_is_not_completed(
    tmp_path: Path,
) -> None:
    result = _run_premerge_blueprint_harness(
        tmp_path, completed=False, blueprint_issue=123
    )

    assert result.returncode == 1
    assert "make docs-blueprint-complete SLUG=2026-08-10-sample" in result.stdout
    assert "merge reached" not in result.stdout


def test_closeout_allows_completed_issue_owned_blueprint_to_reach_merge(
    tmp_path: Path,
) -> None:
    result = _run_premerge_blueprint_harness(
        tmp_path, completed=True, blueprint_issue=123
    )

    assert result.returncode == 42
    assert "merge reached" in result.stdout


def test_closeout_rejects_copied_blueprint_that_remains_active(tmp_path: Path) -> None:
    result = _run_premerge_blueprint_harness(
        tmp_path,
        completed=True,
        blueprint_issue=123,
        duplicate_active=True,
    )

    assert result.returncode == 1
    assert "contains both" in result.stdout
    assert "merge reached" not in result.stdout


def test_closeout_does_not_block_child_issue_for_a_different_blueprint_tracker(
    tmp_path: Path,
) -> None:
    result = _run_premerge_blueprint_harness(
        tmp_path, completed=False, blueprint_issue=999
    )

    assert result.returncode == 42
    assert "merge reached" in result.stdout


def test_pr_must_be_positive_integer_before_any_remote_command(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            *CLOSEOUT_COMMAND,
            "--pr",
            "0",
            "--skill",
            "review",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--pr must be a positive integer" in result.stdout


def test_native_launcher_refuses_nonisolated_python(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/util/pr_closeout.py"),
            "--pr",
            "0",
            "--skill",
            "review",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "invoke with `/usr/bin/python3 -I" in result.stderr


def test_empty_explicit_run_id_is_rejected_before_pr_body_lookup(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(
        "#!/bin/bash\n" 'echo "$*" >>"$CLOSEOUT_CALLS"\n' "exit 99\n",
        encoding="utf-8",
    )
    gh_stub.chmod(0o755)

    result = subprocess.run(
        [
            *CLOSEOUT_COMMAND,
            "--pr",
            "123",
            "--skill",
            "review",
            "--run-id",
            "   ",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CLOSEOUT_CALLS": str(calls),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--run-id must match sr_<32 lowercase hex characters>" in result.stdout
    assert not calls.exists()


def test_closeout_transitions_with_recovered_run_id_and_only_completes_merged() -> None:
    final_ci = 'python3 "$SCRIPT_DIR/final_ci_gate.py"'
    expected_head = (
        'expected_head="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"'
    )
    wait = '--wait --expected-head "$expected_head"'
    locked_merge = '--merge --expected-head "$expected_head"'
    remote_merge_verification = 'if [ "$state" != "MERGED" ]; then'
    reconciliation = "# 3. Post-merge Finding Ledger reconciliation"

    final_ci_index = SCRIPT.index(final_ci)
    expected_head_index = SCRIPT.index(expected_head)
    wait_index = SCRIPT.index(wait)
    merge_index = SCRIPT.index(locked_merge)
    merge_transition = SCRIPT.rindex("transition_closeout", final_ci_index, merge_index)
    remote_verification_index = SCRIPT.index(remote_merge_verification)
    recovery_transition = SCRIPT.index("transition_closeout", remote_verification_index)

    assert (
        expected_head_index
        < final_ci_index
        < wait_index
        < merge_transition
        < merge_index
    )
    assert '--expected-head "$expected_head"' in SCRIPT
    assert (
        remote_verification_index < recovery_transition < SCRIPT.index(reconciliation)
    )
    assert "--verified-merged" in SCRIPT
    assert 'delivery_pipeline.py" "${settlement_args[@]}"' in SCRIPT


def test_closeout_gives_all_gate_consumers_one_git_environment_authority() -> None:
    assert "run_composed_git_authority()" in SCRIPT
    authority_consumers = [
        line.strip()
        for line in SCRIPT.splitlines()
        if (
            '"$SCRIPT_DIR/pr_merge_gate.py"' in line
            or '"$SCRIPT_DIR/final_ci_gate.py"' in line
            or '"$SCRIPT_DIR/delivery_review_risk.py"' in line
            or '"$SCRIPT_DIR/delivery_pipeline.py"' in line
        )
    ]

    assert authority_consumers
    assert all(
        "run_composed_git_authority python3 " in line for line in authority_consumers
    )


def test_closeout_phase_timing_warns_once_instead_of_blocking_delivery() -> None:
    transition = SCRIPT[
        SCRIPT.index("transition_closeout() {") : SCRIPT.index("blueprint_has_issue()")
    ]

    assert 'if ! phase_output="$(python3 "$SCRIPT_DIR/skill_run_log.py"' in transition
    assert "Coarse phase timing was not recorded" in transition
    assert "closeout_transitioned=1" in transition
    assert transition.index("closeout_transitioned=1") < transition.index(
        "if ! phase_output="
    )


def test_closeout_binds_wait_and_merge_to_one_captured_head() -> None:
    capture = 'expected_head="$(gh pr view "$PR" --json headRefOid -q .headRefOid)"'
    wait = '--wait --expected-head "$expected_head"'
    merge = '--merge --expected-head "$expected_head"'

    assert SCRIPT.count(capture) == 1
    assert SCRIPT.count(wait) == 1
    assert SCRIPT.count(merge) == 1
    assert SCRIPT.index(capture) < SCRIPT.index(wait) < SCRIPT.index(merge)


def test_reconciliation_happens_only_after_fetch_and_primary_containment() -> None:
    fetch = 'fetch "$fetch_remote" -q "$remote_default_ref"'
    containment = 'merge-base --is-ancestor "$merge_oid" "$fresh_oid"'
    classify = '"$SCRIPT_DIR/finding_anchor.py"'

    assert fetch in SCRIPT
    assert containment in SCRIPT
    assert SCRIPT.index(fetch) < SCRIPT.index(containment) < SCRIPT.index(classify)


def test_closeout_settles_one_capsule_after_merge_and_before_cleanup() -> None:
    remote_verified = 'if [ "$state" != "MERGED" ]; then'
    settlement = 'delivery_pipeline.py" "${settlement_args[@]}"'
    remote_cleanup = 'remote_ref="refs/heads/$head_ref"'

    assert SCRIPT.index(remote_verified) < SCRIPT.index(settlement)
    assert SCRIPT.index(settlement) < SCRIPT.index(remote_cleanup)
    assert (
        '--reconciliation-continuation "$reconciliation_dir/$reconciliation_prefix-continuation.json"'
        in SCRIPT
    )
    assert 'settlement_capsule="$settlement_dir/pr-$PR-$RUN_ID.json"' in SCRIPT
    assert 'job.sh" binding-files --run-id "$RUN_ID"' in SCRIPT
    assert 'settlement_args+=(--job-binding "$job_binding")' in SCRIPT
    assert "Run log terminalized as the final settlement operation" in SCRIPT
    assert 'skill_run_log.py" "${run_log_args[@]}"' not in SCRIPT


def test_closeout_rejects_outcomes_that_cannot_match_settlement() -> None:
    assert (
        'if [ "$OUTCOME" != "merged" ] && [ "$OUTCOME" != "in_progress" ]; then'
        in SCRIPT
    )
    assert (
        'settlement_args+=(--merge-delta-review-task "$MERGE_DELTA_REVIEW_TASK")'
        in SCRIPT
    )
    assert (
        'settlement_args+=(--merge-delta-review-run-id "$MERGE_DELTA_REVIEW_RUN_ID")'
        in SCRIPT
    )
    assert "--merge-delta-review-run-id requires --merge-delta-review-task" in SCRIPT
    assert '[[ "$MERGE_DELTA_REVIEW_RUN_ID" =~ ^sr_[0-9a-f]{32}$ ]]' in SCRIPT
    assert "cross-run merge-delta review requires explicit --run-id recovery" in SCRIPT


def test_reconciliation_uses_exact_ids_json_cas_and_review_receipt() -> None:
    assert "--extract-finding-ids" in SCRIPT
    assert "filter-known" in SCRIPT
    assert "validate-orphan-trailer-waiver" not in SCRIPT
    assert "orphan-waiver.json" not in SCRIPT
    assert 'elif [ -z "$deposited_ids_output" ]; then' in SCRIPT
    assert "No deposited exact Finding Ledger trailers require reconciliation" in SCRIPT
    assert "--json" in SCRIPT
    assert '--provenance-pr "$PR"' in SCRIPT
    assert '--provenance-commit "$merge_oid"' in SCRIPT
    assert (
        '--operation-id-prefix "pr-closeout:$PR:$merge_oid:lease:$group_lease_id"'
        in SCRIPT
    )
    assert "group_lease_owner_args=(--coordinator-id" in SCRIPT
    assert '"--run-id" "${group_values[2]}"' in SCRIPT
    assert '"--worker-session-id" "${group_values[3]}")' in SCRIPT
    assert "lease owner run_id does not match closeout run" in SCRIPT
    assert '"${group_lease_owner_args[@]}"' in SCRIPT
    assert '--confirmed-by-review-task "$review_task_id"' in SCRIPT


@requires_host_job_authority
def test_closeout_reconciles_two_same_run_leases_and_replays_interruption(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "primary"
    worktree = fixture / "worktree"
    bin_dir = tmp_path / "bin"
    publication = fixture / ".audit/pr-publications/receipt.jsonl"
    worktree.mkdir(parents=True)
    bin_dir.mkdir()
    _write_canonical_config(fixture)
    publication.parent.mkdir(parents=True)
    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":'
        '"review-evidence-phase-a-code-rebase-full-3691"}\n',
        encoding="utf-8",
    )
    calls = tmp_path / "calls.log"
    fail_marker = tmp_path / "failed-second-lease"
    first_id = "f_0123456789abcdefabcd"
    second_id = "f_fedcba9876543210abcd"
    run_id = "sr_" + "a" * 32
    merge_oid = "a" * 40
    head_oid = "b" * 40

    (bin_dir / "git").write_text(
        "#!/bin/bash\n"
        'if [ -n "${GIT_EXEC_PATH:-}${GIT_ASKPASS:-}${HTTPS_PROXY:-}${SSL_CERT_FILE:-}" ]; then\n'
        '  echo "unsafe network environment reached git" >&2; exit 91\n'
        "fi\n"
        'echo "git $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$*" in\n'
        f'  *"rev-parse --path-format=absolute --git-common-dir"*) echo "{fixture}/.git" ;;\n'
        f'  *"rev-parse --show-toplevel"*) echo "{worktree}" ;;\n'
        '  *"rev-parse --verify FETCH_HEAD"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        f'  *"fetch {CANONICAL_ORIGIN} -q refs/heads/main"*)\n'
        f'    [ "${{GIT_CONFIG_VALUE_2:-}}" = "!{bin_dir}/gh auth git-credential" ] || {{ echo "credential overlay missing" >&2; exit 92; }}\n'
        "    ;;\n"
        '  *"symbolic-ref -q --short refs/remotes/origin/HEAD"*) exit 1 ;;\n'
        '  *"merge-base --is-ancestor"*) exit 0 ;;\n'
        f'  *"show -s --format=%B"*) printf "fix: delivered\\n\\nFixes: {first_id}\\nFixes: {second_id}\\n" ;;\n'
        f'  *"log --format=%B%x00"*) printf "fix: delivered\\n\\nFixes: {first_id}\\nFixes: {second_id}\\0" ;;\n'
        '  *"ls-remote --exit-code --heads"*) exit 1 ;;\n'
        '  *"show-ref --verify --quiet"*) exit 1 ;;\n'
        '  *"symbolic-ref -q HEAD"*) echo "refs/heads/main" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "gh").write_text(
        "#!/bin/bash\n"
        'echo "gh $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$*" in\n'
        '  *"--json state -q .state"*) echo "MERGED" ;;\n'
        '  *"--json headRefName -q .headRefName"*) echo "feature/test" ;;\n'
        f'  *"--json headRefOid -q .headRefOid"*) echo "{head_oid}" ;;\n'
        f'  *"--json mergeCommit -q .mergeCommit.oid"*) echo "{merge_oid}" ;;\n'
        '  *) echo "[]" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "python3").write_text(
        "#!/bin/bash\n"
        '[ "$1" = "-P" ] && shift\n'
        'if [ "$1" = "-" ]; then exec /usr/bin/python3 "$@"; fi\n'
        'echo "python3 $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$1" in\n'
        '  *agent_dispatch.py) echo "{}" ;;\n'
        "  *finding_anchor.py)\n"
        '    if [[ "$*" == *"filter-known"* ]]; then\n'
        f'      printf "%s\\n" "{first_id}" "{second_id}"\n'
        '    elif [[ "$*" == *"--apply"* ]]; then\n'
        f'      if [[ "$*" == *"{first_id}"* ]]; then selected="{first_id}"; else selected="{second_id}"; fi\n'
        f'      if [ "$selected" = "{second_id}" ] && [ ! -e "{fail_marker}" ]; then touch "{fail_marker}"; exit 1; fi\n'
        '      printf \'{"findings":[{"finding_id":"%s","severity":"important","proposed":{"state":"addressed"}}]}\\n\' "$selected"\n'
        "    else\n"
        f'      printf \'%s\\n\' \'{{"findings":[{{"finding_id":"{first_id}","severity":"important","lease":{{"lease_id":"fl_one","owner":{{"coordinator_id":"coordinator-one","run_id":"{run_id}","worker_session_id":"worker-one"}}}},"proposed":{{"state":"addressed"}}}},{{"finding_id":"{second_id}","severity":"important","lease":{{"lease_id":"fl_two","owner":{{"coordinator_id":"coordinator-two","run_id":"{run_id}","worker_session_id":"worker-two"}}}},"proposed":{{"state":"addressed"}}}}]}}\'\n'
        "    fi ;;\n"
        '  *pr_merge_gate.py) exec /usr/bin/python3 "$@" ;;\n'
        "  *delivery_pipeline.py) exit 0 ;;\n"
        '  *skill_run_log.py) echo "intelflo-v1" ;;\n'
        '  *) exec /usr/bin/python3 "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    command = [
        *CLOSEOUT_COMMAND,
        "--pr",
        "123",
        "--skill",
        "review",
        "--run-id",
        run_id,
    ]
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CLOSEOUT_CALLS": str(calls),
    }
    interrupted = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    continuation = next(
        (fixture / ".audit/finding-reconciliation").glob("*-continuation.json")
    )
    assert interrupted.returncode == 0, interrupted.stderr
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "CAS, lease, or accepted-review precondition rejected apply for fl_two"
    )

    result = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    apply_calls = [
        line
        for line in calls.read_text(encoding="utf-8").splitlines()
        if "finding_anchor.py" in line and "--apply" in line
    ]
    assert len(apply_calls) == 4
    assert any("--coordinator-id coordinator-one" in line for line in apply_calls)
    assert any("--coordinator-id coordinator-two" in line for line in apply_calls)
    assert any(
        "--operation-id-prefix" in line and ":lease:fl_one" in line
        for line in apply_calls
    )
    assert any(
        "--operation-id-prefix" in line and ":lease:fl_two" in line
        for line in apply_calls
    )
    first_lease_calls = [line for line in apply_calls if ":lease:fl_one" in line]
    assert len(first_lease_calls) == 2
    assert first_lease_calls[0] == first_lease_calls[1]
    assert not list(
        (fixture / ".audit/finding-reconciliation").glob("*-continuation.json")
    )


@requires_host_job_authority
def test_closeout_retries_mandatory_repark_before_fallible_tail(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "primary"
    worktree = fixture / "worktree"
    bin_dir = tmp_path / "bin"
    fetch_marker = tmp_path / "fetch-failed-once"
    worktree.mkdir(parents=True)
    bin_dir.mkdir()
    _write_canonical_config(fixture)
    run_id = "sr_" + "a" * 32
    merge_oid = "a" * 40
    head_oid = "b" * 40
    absent_id = "f_fedcba9876543210abcd"

    (bin_dir / "git").write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        f'  *"rev-parse --path-format=absolute --git-common-dir"*) echo "{fixture}/.git" ;;\n'
        f'  *"rev-parse --show-toplevel"*) echo "{worktree}" ;;\n'
        '  *"rev-parse --verify FETCH_HEAD"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        '  *"symbolic-ref -q --short refs/remotes/origin/HEAD"*) exit 1 ;;\n'
        f'  *"fetch "*) if [ ! -e "{fetch_marker}" ]; then touch "{fetch_marker}"; exit 1; fi ;;\n'
        '  *"merge-base --is-ancestor"*) exit 0 ;;\n'
        f'  *"show -s --format=%B"*) printf "fix: delivered\\n\\nFixes: {absent_id}\\n" ;;\n'
        f'  *"log --format=%B%x00"*) printf "fix: delivered\\n\\nFixes: {absent_id}\\0" ;;\n'
        '  *"ls-remote --exit-code --heads"*) exit 1 ;;\n'
        '  *"show-ref --verify --quiet"*) exit 1 ;;\n'
        '  *"symbolic-ref -q HEAD"*) echo "refs/heads/main" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "gh").write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '  *"--json state -q .state"*) echo "MERGED" ;;\n'
        '  *"--json headRefName -q .headRefName"*) echo "feature/test" ;;\n'
        f'  *"--json headRefOid -q .headRefOid"*) echo "{head_oid}" ;;\n'
        f'  *"--json mergeCommit -q .mergeCommit.oid"*) echo "{merge_oid}" ;;\n'
        '  *) echo "[]" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "python3").write_text(
        "#!/bin/bash\n"
        '[ "$1" = "-P" ] && shift\n'
        'if [ "$1" = "-" ]; then exec /usr/bin/python3 "$@"; fi\n'
        'case "$1" in\n'
        '  *agent_dispatch.py) echo "{}" ;;\n'
        '  *pr_merge_gate.py) exec /usr/bin/python3 "$@" ;;\n'
        '  *skill_run_log.py) echo "intelflo-v1" ;;\n'
        "  *delivery_pipeline.py) exit 0 ;;\n"
        "  *finding_anchor.py)\n"
        '    if [[ "$*" == *"filter-known"* ]]; then\n'
        f'      echo "finding-anchor: warning: skipped absent ID {absent_id}" >&2\n'
        "    else\n"
        '      echo "finding classifier must not run" >&2; exit 99\n'
        "    fi ;;\n"
        '  *) exec /usr/bin/python3 "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    command = [
        *CLOSEOUT_COMMAND,
        "--pr",
        "123",
        "--skill",
        "review",
        "--run-id",
        run_id,
    ]
    environment = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    first = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode != 0
    assert "Primary re-park failed before post-merge settlement" in first.stdout
    assert not list(
        (fixture / ".audit/finding-reconciliation").glob("*-continuation.json")
    )

    reconciliation_dir = fixture / ".audit" / "finding-reconciliation"
    reconciliation_dir.mkdir(parents=True)
    continuation_path = reconciliation_dir / "pr-123-aaaaaaaaaaaa-continuation.json"
    plan_path = reconciliation_dir / "pr-123-aaaaaaaaaaaa-plan.json"
    continuation = {
        "finding_ids": [absent_id],
        "merge_commit": merge_oid,
        "mode": "post-merge-reconciliation-continuation",
        "pr": 123,
        "reason": "different reconciliation blocker",
        "run_id": run_id,
        "terminal": False,
    }
    continuation_path.write_text(json.dumps(continuation), encoding="utf-8")
    plan_path.write_text("{}\n", encoding="utf-8")

    replay = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert replay.returncode == 0, replay.stderr
    assert f"skipped absent ID {absent_id}" in replay.stderr
    assert "Preserved an unrelated or invalid reconciliation continuation" in (
        replay.stdout + replay.stderr
    )
    assert continuation_path.exists()
    assert plan_path.exists()

    continuation["reason"] = "read-only reconciliation plan rejected merge provenance"
    continuation_path.write_text(json.dumps(continuation), encoding="utf-8")
    settled_replay = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert settled_replay.returncode == 0, settled_replay.stderr
    assert not continuation_path.exists()
    assert not plan_path.exists()


def test_missing_review_emits_continuation_without_skipping_cleanup() -> None:
    continuation = "write_reconciliation_continuation"
    cleanup = '--force-with-lease="$remote_ref:$head_oid"'

    assert "one bounded exact-tree review" in SCRIPT
    assert SCRIPT.index(continuation) < SCRIPT.index(cleanup)


def test_merge_commit_must_preserve_every_expected_trailer() -> None:
    assert 'missing_ids+=("$finding_id")' in SCRIPT
    assert "merge commit omitted expected trailers" in SCRIPT


@requires_host_job_authority
def test_closeout_rejects_foreign_lease_owner_and_applies_matching_owner(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "primary"
    worktree = fixture / "worktree"
    bin_dir = tmp_path / "bin"
    publication = fixture / ".audit/pr-publications/receipt.jsonl"
    worktree.mkdir(parents=True)
    bin_dir.mkdir()
    _write_canonical_config(fixture)
    publication.parent.mkdir(parents=True)
    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":'
        '"review-evidence-phase-a-code-rebase-full-3691"}\n',
        encoding="utf-8",
    )
    calls = tmp_path / "calls.log"
    finding_id = "f_0123456789abcdefabcd"
    merge_oid = "a" * 40
    head_oid = "b" * 40

    git_stub = bin_dir / "git"
    git_stub.write_text(
        "#!/bin/bash\n"
        'echo "git $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$*" in\n'
        f'  *"rev-parse --path-format=absolute --git-common-dir"*) echo "{fixture}/.git" ;;\n'
        f'  *"rev-parse --show-toplevel"*) echo "{worktree}" ;;\n'
        '  *"rev-parse --verify FETCH_HEAD"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        '  *"symbolic-ref -q --short refs/remotes/origin/HEAD"*) exit 1 ;;\n'
        '  *"merge-base --is-ancestor"*) exit 0 ;;\n'
        f'  *"show -s --format=%B"*) printf "fix: delivered\\n\\nFixes: {finding_id}\\n" ;;\n'
        f'  *"log --format=%B%x00"*) printf "fix: delivered\\n\\nFixes: {finding_id}\\0" ;;\n'
        '  *"ls-remote --exit-code --heads"*) exit 1 ;;\n'
        '  *"show-ref --verify --quiet"*) exit 1 ;;\n'
        '  *"symbolic-ref -q HEAD"*) echo "refs/heads/main" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh_stub = bin_dir / "gh"
    gh_stub.write_text(
        "#!/bin/bash\n"
        'echo "gh $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$*" in\n'
        '  *"--json state -q .state"*) echo "MERGED" ;;\n'
        '  *"--json headRefName -q .headRefName"*) echo "feature/test" ;;\n'
        f'  *"--json headRefOid -q .headRefOid"*) echo "{head_oid}" ;;\n'
        f'  *"--json mergeCommit -q .mergeCommit.oid"*) echo "{merge_oid}" ;;\n'
        '  *) echo "[]" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    python_stub = bin_dir / "python3"
    # Valid-trailer behavior is owned by pr_merge_gate.extract_exact_finding_ids.
    python_stub.write_text(
        "#!/bin/bash\n"
        '[ "$1" = "-P" ] && shift\n'
        'if [ "$1" = "-" ]; then exec /usr/bin/python3 "$@"; fi\n'
        'echo "python3 $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$1" in\n'
        '  *agent_dispatch.py) echo "{}" ;;\n'
        "  *finding_anchor.py)\n"
        f'    if [[ "$*" == *"filter-known"* ]]; then echo "{finding_id}"; else\n'
        f'      printf \'%s\\n\' \'{{"findings":[{{"finding_id":"{finding_id}","severity":"important","lease":{{"lease_id":"fl_exact","owner":{{"coordinator_id":"closeout-coordinator","run_id":"sr_{"a" * 32}","worker_session_id":"closeout-worker"}}}},"proposed":{{"state":"addressed"}}}},{{"finding_id":"f_fedcba9876543210abcd","severity":"suggestion","original_severity":"important","proposed":{{"state":"open"}}}}]}}\'\n'
        '    fi ;;\n'
        '  *pr_merge_gate.py) if [[ "$*" == *"--extract-finding-ids"* ]]; then /usr/bin/sed -n \'s/^Fixes: \\(f_[0-9a-f]\\{20\\}\\)$/\\1/p\'; else echo "unexpected pr_merge_gate invocation" >&2; exit 90; fi ;;\n'
        "  *delivery_pipeline.py) exit 0 ;;\n"
        '  *skill_run_log.py) echo "intelflo-v1" ;;\n'
        '  *) exec /usr/bin/python3 "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    for stub in (git_stub, gh_stub, python_stub):
        stub.chmod(0o755)

    foreign_command = [
        *CLOSEOUT_COMMAND,
        "--pr",
        "123",
        "--skill",
        "review",
        "--run-id",
        "sr_" + "b" * 32,
    ]
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CLOSEOUT_CALLS": str(calls),
    }
    foreign_result = subprocess.run(
        foreign_command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert foreign_result.returncode == 0, foreign_result.stderr
    foreign_invocations = calls.read_text(encoding="utf-8")
    assert "--apply" not in foreign_invocations
    foreign_continuation = next(
        (fixture / ".audit/finding-reconciliation").glob("*-continuation.json")
    )
    assert json.loads(foreign_continuation.read_text(encoding="utf-8"))["reason"] == (
        "lease owner run_id does not match closeout run"
    )

    command = [*foreign_command[:-1], "sr_" + "a" * 32]
    result = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    invocations = calls.read_text(encoding="utf-8")[len(foreign_invocations) :]
    plan = invocations.index(
        "--confirmed-by-review-task review-evidence-phase-a-code-rebase-full-3691"
    )
    apply = invocations.index("--apply")
    assert plan < apply
    assert "--lease-owner" not in invocations
    assert "--coordinator-id closeout-coordinator" in invocations
    assert "--run-id sr_" + "a" * 32 in invocations
    assert "--worker-session-id closeout-worker" in invocations
    assert f"--operation-id-prefix pr-closeout:123:{merge_oid}" in invocations
    assert "merge-base --is-ancestor" in invocations
    continuation = next(
        (fixture / ".audit/finding-reconciliation").glob("*-continuation.json")
    )
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "1 important/critical finding(s) require one bounded exact-tree review"
    )

    calls_before_republication = calls.read_text(encoding="utf-8")
    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":"review-old",'
        f'"expected_head":"{"c" * 40}"}}\n'
        '{"pr":123,"status":"published","review_task_id":"review-current",'
        f'"expected_head":"{head_oid}"}}\n',
        encoding="utf-8",
    )
    republished_result = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert republished_result.returncode == 0, republished_result.stderr
    republished_calls = calls.read_text(encoding="utf-8")[
        len(calls_before_republication) :
    ]
    assert "--confirmed-by-review-task review-current" in republished_calls
    assert "--confirmed-by-review-task review-old" not in republished_calls

    calls_before_exemption = calls.read_text(encoding="utf-8")
    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":null,'
        '"review_exemption":"T0",'
        f'"expected_head":"{head_oid}"}}\n',
        encoding="utf-8",
    )
    exempt_result = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert exempt_result.returncode == 0, exempt_result.stderr
    exempt_calls = calls.read_text(encoding="utf-8")[len(calls_before_exemption) :]
    assert "--confirmed-by-review-task" not in exempt_calls
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "1 important/critical finding(s) require one bounded exact-tree review"
    )

    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":null,'
        '"review_exemption":"T0"}\n',
        encoding="utf-8",
    )
    unbound_exemption = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unbound_exemption.returncode == 0, unbound_exemption.stderr
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "accepted-review publication receipts could not be read"
    )

    continuation.write_text('{"reason":"sentinel"}\n', encoding="utf-8")
    calls_before_conflicting_exemption = calls.read_text(encoding="utf-8")
    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":"review-current",'
        '"review_exemption":"T0",'
        f'"expected_head":"{head_oid}"}}\n',
        encoding="utf-8",
    )
    conflicting_exemption = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert conflicting_exemption.returncode == 0, conflicting_exemption.stderr
    conflicting_exemption_calls = calls.read_text(encoding="utf-8")[
        len(calls_before_conflicting_exemption) :
    ]
    assert "--confirmed-by-review-task" not in conflicting_exemption_calls
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "accepted-review publication receipts could not be read"
    )

    continuation.write_text('{"reason":"sentinel"}\n', encoding="utf-8")
    calls_before_unknown_exemption = calls.read_text(encoding="utf-8")
    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":null,'
        '"review_exemption":"unknown",'
        f'"expected_head":"{head_oid}"}}\n',
        encoding="utf-8",
    )
    unknown_exemption = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unknown_exemption.returncode == 0, unknown_exemption.stderr
    unknown_exemption_calls = calls.read_text(encoding="utf-8")[
        len(calls_before_unknown_exemption) :
    ]
    assert "--confirmed-by-review-task" not in unknown_exemption_calls
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "accepted-review publication receipts could not be read"
    )

    calls_before_conflict = calls.read_text(encoding="utf-8")
    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":"review-current-a",'
        f'"expected_head":"{head_oid}"}}\n'
        '{"pr":123,"status":"published","review_task_id":"review-current-b",'
        f'"expected_head":"{head_oid}"}}\n',
        encoding="utf-8",
    )
    conflicting_result = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert conflicting_result.returncode == 0, conflicting_result.stderr
    conflicting_calls = calls.read_text(encoding="utf-8")[len(calls_before_conflict) :]
    assert "--confirmed-by-review-task" not in conflicting_calls
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "accepted-review publication receipts could not be read"
    )

    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":"review-null",'
        '"expected_head":null}\n',
        encoding="utf-8",
    )
    null_head_result = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert null_head_result.returncode == 0, null_head_result.stderr
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "accepted-review publication receipts could not be read"
    )

    calls_before_corruption = calls.read_text(encoding="utf-8")
    publication.write_text("{not-json}\n", encoding="utf-8")
    corrupted_result = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert corrupted_result.returncode == 0, corrupted_result.stderr
    calls_after_corruption = calls.read_text(encoding="utf-8")
    new_calls = calls_after_corruption[len(calls_before_corruption) :]
    assert (
        f"git ls-remote --heads {CANONICAL_ORIGIN} refs/heads/feature/test" in new_calls
    )
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "accepted-review publication receipts could not be read"
    )

    publication.write_text(
        '{"pr":123,"status":"published","review_task_id":"review/exact"}\n',
        encoding="utf-8",
    )
    invalid_task_result = subprocess.run(
        command,
        cwd=worktree,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert invalid_task_result.returncode == 0, invalid_task_result.stderr
    assert json.loads(continuation.read_text(encoding="utf-8"))["reason"] == (
        "accepted-review publication receipts could not be read"
    )


def _run_closeout_primary_repark_harness(
    tmp_path: Path,
    *,
    primary_status: str,
    primary_status_after_fetch: str | None = None,
    primary_lease_available: bool = True,
) -> tuple[subprocess.CompletedProcess[str], str]:
    primary = tmp_path / "primary"
    worktree = primary / "worktree"
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "calls.log"
    worktree.mkdir(parents=True)
    bin_dir.mkdir()
    _write_canonical_config(primary)

    (bin_dir / "git").write_text(
        "#!/bin/bash\n"
        'if [ -n "${GIT_EXEC_PATH:-}${GIT_ASKPASS:-}${HTTPS_PROXY:-}${SSL_CERT_FILE:-}" ]; then\n'
        '  echo "unsafe network environment reached git" >&2; exit 91\n'
        "fi\n"
        'echo "git $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$*" in\n'
        f'  *"rev-parse --path-format=absolute --git-common-dir"*) echo "{primary}/.git" ;;\n'
        f'  *"rev-parse --show-toplevel"*) echo "{worktree}" ;;\n'
        '  *"rev-parse --verify FETCH_HEAD"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        '  *"merge-base refs/remotes/origin/main bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        f'  *"fetch {CANONICAL_ORIGIN} -q refs/heads/main"*)\n'
        f'    [ "${{GIT_CONFIG_VALUE_2:-}}" = "!{bin_dir}/gh auth git-credential" ] || {{ echo "credential overlay missing" >&2; exit 92; }}\n'
        "    ;;\n"
        '  *"symbolic-ref -q --short refs/remotes/origin/HEAD"*) exit 1 ;;\n'
        '  *"merge-base --is-ancestor"*) exit 0 ;;\n'
        '  *"show -s --format=%B"*|*"log --format=%B%x00"*) printf "fix: delivered\\n" ;;\n'
        '  *"ls-remote --exit-code --heads"*) exit 1 ;;\n'
        '  *"show-ref --verify --quiet"*) exit 1 ;;\n'
        '  *"status --porcelain"*)\n'
        '    if [ -e "$CLOSEOUT_STATUS_MARKER" ]; then\n'
        "      printf '%s' \"$CLOSEOUT_PRIMARY_STATUS_AFTER_FETCH\"\n"
        "    else\n"
        '      : >"$CLOSEOUT_STATUS_MARKER"\n'
        "      printf '%s' \"$CLOSEOUT_PRIMARY_STATUS\"\n"
        "    fi\n"
        "    ;;\n"
        '  *"symbolic-ref -q HEAD"*) echo "refs/heads/primary-branch" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "gh").write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '  *"--json state -q .state"*) echo "MERGED" ;;\n'
        '  *"--json headRefName -q .headRefName"*) echo "feature/test" ;;\n'
        '  *"--json headRefOid -q .headRefOid"*) echo "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" ;;\n'
        '  *"--json mergeCommit -q .mergeCommit.oid"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        '  *) echo "[]" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "python3").write_text(
        "#!/bin/bash\n"
        '[ "$1" = "-P" ] && shift\n'
        'case "$1" in\n'
        '  *agent_dispatch.py) echo "{}" ;;\n'
        "  *worktree_guard.py)\n"
        '    if [ "$CLOSEOUT_PRIMARY_LEASE_AVAILABLE" = "0" ]; then\n'
        '      echo "worktree-guard error: worktree lease held by another writer" >&2\n'
        "      exit 2\n"
        "    fi\n"
        '    echo "lease acquired" >>"$CLOSEOUT_CALLS"\n'
        "    shift\n"
        '    while [ "$1" != "--" ]; do shift; done\n'
        "    shift\n"
        '    "$@"\n'
        "    status=$?\n"
        '    echo "lease released" >>"$CLOSEOUT_CALLS"\n'
        "    exit $status\n"
        "    ;;\n"
        "  *delivery_pipeline.py) exit 0 ;;\n"
        '  *skill_run_log.py) echo "intelflo-v1" ;;\n'
        '  *) exec /usr/bin/python3 "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    result = subprocess.run(
        [
            *CLOSEOUT_COMMAND,
            "--pr",
            "123",
            "--skill",
            "review",
            "--run-id",
            "sr_" + "a" * 32,
        ],
        cwd=worktree,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CLOSEOUT_CALLS": str(calls),
            "CLOSEOUT_PRIMARY_STATUS": primary_status,
            "CLOSEOUT_PRIMARY_STATUS_AFTER_FETCH": (
                primary_status
                if primary_status_after_fetch is None
                else primary_status_after_fetch
            ),
            "CLOSEOUT_STATUS_MARKER": str(tmp_path / "status-called"),
            "CLOSEOUT_PRIMARY_LEASE_AVAILABLE": "1" if primary_lease_available else "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text(encoding="utf-8")


@requires_host_job_authority
def test_closeout_reparks_clean_primary_on_a_branch(tmp_path: Path) -> None:
    result, calls = _run_closeout_primary_repark_harness(tmp_path, primary_status="")

    assert result.returncode == 0, result.stderr
    assert "checkout -q --detach aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in calls
    assert "Primary re-parked on fresh origin/main" in result.stdout


@requires_host_job_authority
def test_closeout_reparks_primary_before_fallible_settlement(tmp_path: Path) -> None:
    result, calls = _run_closeout_primary_repark_harness(tmp_path, primary_status="")

    assert result.returncode == 0, result.stderr
    assert calls.index(
        "checkout -q --detach aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ) < calls.index("ls-remote --heads")


@requires_host_job_authority
def test_closeout_holds_primary_lease_across_the_repark_mutation_span(
    tmp_path: Path,
) -> None:
    result, calls = _run_closeout_primary_repark_harness(tmp_path, primary_status="")

    assert result.returncode == 0, result.stderr
    lease_acquired = calls.index("lease acquired")
    initial_status = calls.index(
        "status --porcelain --untracked-files=all", lease_acquired
    )
    refresh = calls.index(
        f"fetch {CANONICAL_ORIGIN} -q refs/heads/main", initial_status
    )
    second_status = calls.index("status --porcelain --untracked-files=all", refresh)
    checkout = calls.index(
        "checkout -q --detach aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        second_status,
    )
    lease_released = calls.index("lease released", checkout)
    assert (
        lease_acquired
        < initial_status
        < refresh
        < second_status
        < checkout
        < lease_released
    )
    assert calls.count("status --porcelain --untracked-files=all") == 2


def test_closeout_does_not_repark_dirty_primary(tmp_path: Path) -> None:
    result, calls = _run_closeout_primary_repark_harness(
        tmp_path, primary_status=" M protected-file\n"
    )

    assert result.returncode != 0
    assert "fetch origin -q" not in calls
    assert "checkout -q --detach aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in calls
    assert "Primary re-park failed — canonical primary is dirty" in result.stderr


def test_closeout_rechecks_primary_cleanliness_after_fetch(tmp_path: Path) -> None:
    result, calls = _run_closeout_primary_repark_harness(
        tmp_path,
        primary_status="",
        primary_status_after_fetch=" M protected-file\n",
    )

    assert result.returncode != 0
    assert f"fetch {CANONICAL_ORIGIN} -q refs/heads/main" in calls
    assert "checkout -q --detach aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in calls
    assert (
        "Primary re-park failed — canonical primary became dirty during refresh"
        in result.stderr
    )


def test_closeout_skips_repark_when_primary_lease_is_unavailable(
    tmp_path: Path,
) -> None:
    result, calls = _run_closeout_primary_repark_harness(
        tmp_path,
        primary_status="",
        primary_lease_available=False,
    )

    assert result.returncode != 0
    assert "status --porcelain --untracked-files=all" not in calls
    assert "fetch origin -q" not in calls
    assert "checkout -q --detach aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in calls
    assert "worktree lease held by another writer" in result.stderr


def _run_outcome_admission_harness(
    tmp_path: Path,
    *,
    outcome: str | None = "in_progress",
    notes: str | None = "post-merge-acceptance:qa-2644",
    issue: int | None = 123,
    issue_state: str = "OPEN",
    issue_state_after_ci: str = "",
    pr_body: str = "Refs #123",
    merge: bool = True,
    skill: str = "work-issue",
    active_run_id: str = "sr_" + "a" * 32,
    run_owner_skill: str | None = "work-issue",
    origin_url: str | None = None,
    mutate_config_before_run: bool = False,
    add_branch_config_before_run: bool = False,
    remote_head_oid: str = "",
    local_head_oid: str = "",
    extra_env: dict[str, str] | None = None,
    config_suffix: str = "",
    require_composed_fetch_credentials: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Exercise closeout admission and its complete shell tail without GitHub writes."""
    primary = tmp_path / "primary"
    worktree = primary / "worktree"
    bin_dir = tmp_path / "bin"
    calls = tmp_path / "calls.log"
    merged_marker = tmp_path / "merged.marker"
    body_file = tmp_path / "pr_body.txt"
    worktree.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(exist_ok=True)
    config_path = _write_canonical_config(primary)
    if config_suffix:
        config_path.write_text(
            config_path.read_text(encoding="utf-8") + config_suffix,
            encoding="utf-8",
        )
    body_file.write_text(pr_body, encoding="utf-8")
    skill_run_dir = primary / ".audit" / "skill-runs"
    skill_run_dir.mkdir(parents=True, exist_ok=True)
    if run_owner_skill is not None:
        (skill_run_dir / "2026-08-11.jsonl").write_text(
            json.dumps(
                {
                    "run_id": "sr_" + "a" * 32,
                    "skill": run_owner_skill,
                    "outcome": "in_progress",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    (bin_dir / "git").write_text(
        "#!/bin/bash\n"
        'if [ -n "${GIT_EXEC_PATH:-}${GIT_ASKPASS:-}${HTTPS_PROXY:-}${SSL_CERT_FILE:-}" ]; then\n'
        '  echo "unsafe network environment reached git" >&2; exit 91\n'
        "fi\n"
        'echo "git $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$*" in\n'
        f'  *"rev-parse --path-format=absolute --git-common-dir"*) echo "{primary}/.git" ;;\n'
        f'  *"rev-parse --show-toplevel"*) echo "{worktree}" ;;\n'
        '  *"rev-parse --verify FETCH_HEAD"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        '  *"merge-base refs/remotes/origin/main bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        '  *"symbolic-ref -q --short refs/remotes/origin/HEAD"*) exit 1 ;;\n'
        '  *"merge-base --is-ancestor"*) exit 0 ;;\n'
        '  *"show -s --format=%B"*) printf "merge commit\n" ;;\n'
        '  *"log --format=%B%x00"*) printf "delivery commit\\0" ;;\n'
        '  *"ls-remote --heads"*)\n'
        '    if [ -n "$CLOSEOUT_REMOTE_HEAD_OID" ]; then\n'
        '      printf "%s\\trefs/heads/feature/test\\n" "$CLOSEOUT_REMOTE_HEAD_OID"\n'
        "    fi\n"
        "    ;;\n"
        '  *"show-ref --verify --quiet refs/heads/feature/test"*)\n'
        '    [ -n "$CLOSEOUT_LOCAL_HEAD_OID" ] && [ ! -e "$CLOSEOUT_LOCAL_DELETED_MARKER" ]\n'
        "    ;;\n"
        '  *"worktree list --porcelain"*) : ;;\n'
        '  *"update-ref -d refs/heads/feature/test "*)\n'
        '    expected_old="${*: -1}"\n'
        '    [ "$expected_old" = "$CLOSEOUT_LOCAL_HEAD_OID" ] || exit 1\n'
        '    : >"$CLOSEOUT_LOCAL_DELETED_MARKER"\n'
        "    ;;\n"
        '  *"status --porcelain"*) : ;;\n'
        '  *"fetch "*)\n'
        f'    if [ "$CLOSEOUT_REQUIRE_COMPOSED_FETCH_CREDENTIALS" = "1" ] && [[ "$*" == *"fetch --quiet {CANONICAL_ORIGIN} +refs/heads/main:refs/remotes/origin/main"* ]]; then\n'
        '      [ -z "${GIT_CONFIG_COUNT:-}" ] || { echo "caller credential overlay reached composed fetch" >&2; exit 92; }\n'
        '      expected="-c core.hooksPath=/dev/null -c credential.https://github.com.helper= -c credential.https://github.com.helper=!$CLOSEOUT_EXPECTED_GH auth git-credential fetch "\n'
        '      [[ "$*" == *"$expected"* ]] || { echo "composed fetch credential helper missing" >&2; exit 93; }\n'
        "    fi\n"
        "    ;;\n"
        '  *"checkout -q --detach"*) : ;;\n'
        "  *) : ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "gh").write_text(
        "#!/bin/bash\n"
        'echo "gh $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$*" in\n'
        '  *"issue view "*"--json state -q .state"*)\n'
        '    n=$(($(cat "$CLOSEOUT_ISSUE_VIEW_COUNT" 2>/dev/null || echo 0)+1))\n'
        '    echo "$n" >"$CLOSEOUT_ISSUE_VIEW_COUNT"\n'
        '    if [ -n "$CLOSEOUT_ISSUE_STATE_AFTER_CI" ] && [ "$n" -ge 2 ]; then\n'
        '      echo "$CLOSEOUT_ISSUE_STATE_AFTER_CI"\n'
        "    else\n"
        '      echo "$CLOSEOUT_ISSUE_STATE"\n'
        "    fi\n"
        "    ;;\n"
        '  *"pr view "*"--json body -q .body"*) cat "$CLOSEOUT_PR_BODY_FILE" ;;\n'
        '  *"pr view "*"--json state -q .state"*)\n'
        '    if [ -e "$CLOSEOUT_MERGED_MARKER" ]; then echo "MERGED"; else echo "$CLOSEOUT_PR_STATE"; fi\n'
        "    ;;\n"
        '  *"--json headRefName -q .headRefName"*) echo "feature/test" ;;\n'
        '  *"--json baseRefName -q .baseRefName"*) echo "main" ;;\n'
        '  *"--json headRefOid -q .headRefOid"*) echo "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" ;;\n'
        '  *"--json mergeCommit -q .mergeCommit.oid"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        '  *"api repos/{owner}/{repo}/pulls/123 --jq .base.sha"*) echo "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;\n'
        '  *) echo "[]" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    (bin_dir / "python3").write_text(
        "#!/bin/bash\n"
        '[ "$1" = "-P" ] && shift\n'
        'echo "python3 $*" >>"$CLOSEOUT_CALLS"\n'
        'case "$1" in\n'
        "  *agent_dispatch.py)\n"
        '    if [[ "$*" == *"active-run-id"* ]]; then echo "$CLOSEOUT_ACTIVE_RUN_ID"; else echo "lifecycle complete"; fi\n'
        "    ;;\n"
        '  *delivery_review_risk.py) echo \'{"effective_tier":"T1"}\' ;;\n'
        "  *final_ci_gate.py)\n"
        '    if [[ "$*" == *"--workflow-preflight-only"* ]]; then exit 0; fi\n'
        '    echo "final CI reached"; exit "${CLOSEOUT_FINAL_CI_EXIT:-0}"\n'
        "    ;;\n"
        "  *pr_merge_gate.py)\n"
        '    if [[ "$*" == *"--merge"* ]]; then : >"$CLOSEOUT_MERGED_MARKER"; fi\n'
        "    ;;\n"
        "  *system_error_issue_lock.py) : ;;\n"
        '  *delivery_pipeline.py) echo "settlement reached" ;;\n'
        "  *skill_run_log.py)\n"
        '    if [[ "$*" == *"--check-owner"* ]] && [ -n "$CLOSEOUT_SKILL_LOG_FAILURE" ]; then\n'
        '      echo "skill run authority unreadable" >&2; exit "$CLOSEOUT_SKILL_LOG_FAILURE"\n'
        "    fi\n"
        '    if [[ "$*" == *"--check-owner"* ]] && [ -z "$CLOSEOUT_RUN_OWNER_SKILL" ]; then\n'
        '      if [[ "$*" == *"--allow-missing-owner"* ]]; then echo "intelflo-v1"; exit 0; else echo "unknown run_id" >&2; exit 2; fi\n'
        "    fi\n"
        '    if [[ "$*" == *"--check-owner"* ]] && [[ "$*" != *"--skill $CLOSEOUT_RUN_OWNER_SKILL"* ]]; then\n'
        '      echo "skill_run_log.py: error: run_id is already owned by $CLOSEOUT_RUN_OWNER_SKILL" >&2; exit 2\n'
        "    fi\n"
        '    if [[ "$*" == *"--print-delivery-contract"* ]]; then echo "${CLOSEOUT_DELIVERY_CONTRACT:-intelflo-v1}"; else echo "skill run log reached"; fi\n'
        "    ;;\n"
        '  *reentry_capsule.py) echo "reentry capsule reached" ;;\n'
        "  *worktree_guard.py)\n"
        "    shift\n"
        '    while [ "$1" != "--" ]; do shift; done\n'
        "    shift\n"
        '    "$@"\n'
        "    ;;\n"
        '  *) exec /usr/bin/python3 "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    args = [
        *CLOSEOUT_COMMAND,
        "--pr",
        "123",
        "--skill",
        skill,
        "--run-id",
        "sr_" + "a" * 32,
    ]
    if issue is not None:
        args.extend(["--issue", str(issue)])
    if merge:
        args.append("--merge")
    if outcome is not None:
        args.extend(["--outcome", outcome])
    if notes is not None:
        args.extend(["--notes", notes])
    if origin_url is not None:
        config_digest = _git_config_security_digest(config_path)
        if mutate_config_before_run:
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + '[url "https://attacker.invalid/rebound.git"]\n'
                + f"\tinsteadOf = {origin_url}\n",
                encoding="utf-8",
            )
        if add_branch_config_before_run:
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + '[branch "concurrent"]\n'
                + "\tremote = origin\n"
                + "\tmerge = refs/heads/main\n",
                encoding="utf-8",
            )
        args.extend(
            [
                "--origin-url",
                origin_url,
                "--git-config-sha256",
                config_digest,
            ]
        )

    return subprocess.run(
        args,
        cwd=worktree,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CLOSEOUT_CALLS": str(calls),
            "CLOSEOUT_ISSUE_STATE": issue_state,
            "CLOSEOUT_ISSUE_STATE_AFTER_CI": issue_state_after_ci,
            "CLOSEOUT_ISSUE_VIEW_COUNT": str(tmp_path / "issue-view-count"),
            "CLOSEOUT_PR_STATE": "OPEN",
            "CLOSEOUT_MERGED_MARKER": str(merged_marker),
            "CLOSEOUT_PR_BODY_FILE": str(body_file),
            "CLOSEOUT_ACTIVE_RUN_ID": active_run_id,
            "CLOSEOUT_RUN_OWNER_SKILL": run_owner_skill or "",
            "CLOSEOUT_SKILL_LOG_FAILURE": "",
            "CLOSEOUT_FINAL_CI_EXIT": "0",
            "CLOSEOUT_REMOTE_HEAD_OID": remote_head_oid,
            "CLOSEOUT_LOCAL_HEAD_OID": local_head_oid,
            "CLOSEOUT_LOCAL_DELETED_MARKER": str(tmp_path / "local-deleted"),
            "CLOSEOUT_REQUIRE_COMPOSED_FETCH_CREDENTIALS": (
                "1" if require_composed_fetch_credentials else "0"
            ),
            "CLOSEOUT_EXPECTED_GH": str(bin_dir / "gh"),
            **(
                _reviewed_main_bootstrap_environment(primary, worktree) if merge else {}
            ),
            **(extra_env or {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_closeout_stops_before_merge_when_final_ci_retry_admission_refuses(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        extra_env={"CLOSEOUT_FINAL_CI_EXIT": "1"},
    )

    assert result.returncode == 1
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "delivery_review_risk.py" in calls
    assert "final_ci_gate.py" in calls
    lines = calls.splitlines()
    preflight = next(
        i for i, line in enumerate(lines) if "--workflow-preflight-only" in line
    )
    risk = next(i for i, line in enumerate(lines) if "delivery_review_risk.py" in line)
    dispatch = next(
        i
        for i, line in enumerate(lines)
        if "final_ci_gate.py" in line and "--expected-head" in line
    )
    assert preflight < risk < dispatch
    assert "--expected-head bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in calls
    assert "--expected-base aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in calls
    assert not any("pr_merge_gate.py --pr 123" in line for line in lines[dispatch:])
    assert not (tmp_path / "merged.marker").exists()


def test_closeout_composed_base_fetch_constructs_credentials_after_scrubbing_overlay(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        require_composed_fetch_credentials=True,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert (
        "git -c core.hooksPath=/dev/null "
        "-c credential.https://github.com.helper= "
        "-c credential.https://github.com.helper=!"
        f"{tmp_path / 'bin/gh'} auth git-credential fetch "
    ) in calls


def test_closeout_binds_all_git_network_calls_and_merge_gate_to_exact_url(
    tmp_path: Path,
) -> None:
    pinned = "https://github.com/feder-positronics/intelflo.git"
    result = _run_outcome_admission_harness(tmp_path, origin_url=pinned)

    assert result.returncode == 0, result.stderr
    calls_path = tmp_path / "calls.log"
    calls = (
        calls_path.read_text(encoding="utf-8").splitlines()
        if calls_path.exists()
        else []
    )
    git_network = [
        line
        for line in calls
        if line.startswith("git ")
        and any(token in line.split() for token in ("fetch", "push", "ls-remote"))
    ]
    assert git_network
    assert all(pinned in line for line in git_network)
    merge_gate = [
        line
        for line in calls
        if "pr_merge_gate.py" in line and " --pr 123 " in f" {line} "
    ]
    assert merge_gate
    assert all(f"--origin-url {pinned}" in line for line in merge_gate)
    assert all("--git-config-sha256 " in line for line in merge_gate)
    final_ci = [line for line in calls if "final_ci_gate.py" in line]
    assert final_ci
    assert all(f"--origin-url {pinned}" in line for line in final_ci)
    assert all("--git-config-sha256 " in line for line in final_ci)
    review_risk = [line for line in calls if "delivery_review_risk.py" in line]
    assert review_risk
    assert all(f"--origin-url {pinned}" in line for line in review_risk)
    assert all("--git-config-sha256 " in line for line in review_risk)


def test_closeout_rejects_wrong_skill_before_lifecycle_merge_or_settlement(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        outcome=None,
        skill="execute-blueprint",
        run_owner_skill="work-issue",
    )

    assert result.returncode == 2
    assert "run_id is already owned by work-issue" in result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "agent_dispatch.py" not in calls
    assert "final_ci_gate.py" not in calls
    assert "pr_merge_gate.py" not in calls
    assert "delivery_pipeline.py" not in calls
    assert not (tmp_path / "merged.marker").exists()


def test_closeout_preserves_owner_validator_infrastructure_failure(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        outcome=None,
        extra_env={"CLOSEOUT_SKILL_LOG_FAILURE": "7"},
    )

    assert result.returncode == 7
    assert "skill run authority unreadable" in result.stderr
    assert "Could not validate the closeout run owner" in result.stdout
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "agent_dispatch.py" not in calls
    assert "final_ci_gate.py" not in calls
    assert "pr_merge_gate.py" not in calls
    assert "delivery_pipeline.py" not in calls


@requires_host_job_authority
def test_explicit_historical_recovery_allows_a_missing_run_owner(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        outcome=None,
        run_owner_skill=None,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "skill_run_log.py --skill work-issue" in calls
    assert "--check-owner --print-delivery-contract --allow-missing-owner" in calls
    assert "delivery_pipeline.py settle" in calls


def test_closeout_derives_and_exports_canonical_github_binding(tmp_path: Path) -> None:
    result = _run_outcome_admission_harness(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "GH_HOST conflicts" not in result.stdout + result.stderr
    assert 'export GH_HOST="github.com"' in SCRIPT
    assert 'export GH_REPO="github.com/$repository_slug"' in SCRIPT
    assert "export GIT_CONFIG_COUNT=5" in SCRIPT
    assert 'export GIT_CONFIG_KEY_0="core.hooksPath"' in SCRIPT
    assert "export GIT_CONFIG_VALUE_0=/dev/null" in SCRIPT
    assert 'export GIT_CONFIG_KEY_1="credential.https://github.com.helper"' in SCRIPT
    assert 'export GIT_CONFIG_VALUE_1=""' in SCRIPT
    assert 'export GIT_CONFIG_VALUE_2="!$GH_BIN auth git-credential"' in SCRIPT
    assert "export GIT_TERMINAL_PROMPT=0" in SCRIPT
    assert "export GIT_NO_REPLACE_OBJECTS=1" in SCRIPT
    assert "gh()" in SCRIPT
    assert "assert_git_config_binding || return $?" in SCRIPT
    assert "git_config_origin_url" in SCRIPT
    assert "section = tolower" not in SCRIPT
    assert 'if ! actual_digest="$(git_config_security_digest)"' in SCRIPT


def test_closeout_scrubs_execution_and_transport_environment_before_git(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        extra_env={
            "GIT_EXEC_PATH": "/tmp/attacker-git",
            "GIT_ASKPASS": "/tmp/attacker-askpass",
            "HTTPS_PROXY": "https://attacker.invalid",
            "SSL_CERT_FILE": "/tmp/attacker-ca",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "unsafe network environment reached git" not in result.stderr


def test_closeout_digest_guard_never_falls_through_to_git_in_conditionals(
    tmp_path: Path,
) -> None:
    pinned = "https://github.com/feder-positronics/intelflo.git"
    result = _run_outcome_admission_harness(
        tmp_path,
        origin_url=pinned,
        mutate_config_before_run=True,
    )

    assert result.returncode != 0
    calls_path = tmp_path / "calls.log"
    calls = (
        calls_path.read_text(encoding="utf-8").splitlines()
        if calls_path.exists()
        else []
    )
    git_network = [
        line
        for line in calls
        if line.startswith("git ")
        and any(token in line.split() for token in ("fetch", "push", "ls-remote"))
    ]
    assert git_network == []
    assert "unmeasured execution, redirect" in result.stdout


def test_closeout_allows_concurrent_branch_bookkeeping_before_network(
    tmp_path: Path,
) -> None:
    pinned = "https://github.com/feder-positronics/intelflo.git"
    result = _run_outcome_admission_harness(
        tmp_path,
        origin_url=pinned,
        add_branch_config_before_run=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Pinned Git configuration changed" not in result.stdout + result.stderr


def test_closeout_rejects_unmeasured_git_config_environment(tmp_path: Path) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        extra_env={"GIT_CONFIG_COUNT": "1"},
    )

    assert result.returncode == 2
    assert "GIT_CONFIG_COUNT is not allowed" in result.stdout
    calls_path = tmp_path / "calls.log"
    calls = (
        calls_path.read_text(encoding="utf-8").splitlines()
        if calls_path.exists()
        else []
    )
    assert not any(
        line.startswith("git ")
        and any(token in line.split() for token in ("fetch", "push", "ls-remote"))
        for line in calls
    )


@pytest.mark.parametrize(
    "unsafe_config",
    [
        "[Extensions]\nworktreeConfig = false\n",
        "[extensions]\nworktreeConfig = yes\n",
        "[extensions]\nworktreeConfig\n",
        "[url.attacker]\ninsteadOf = https://github.com/\n",
        '[URL "attacker"]\ninsteadOf = https://github.com/\n',
        '[url\t"attacker"]\ninsteadOf = https://github.com/\n',
        '[IncludeIf "gitdir:/tmp/attacker"]\npath = /tmp/attacker.cfg\n',
        "[core]\nhooksPath = /tmp/attacker-hooks\n",
        "[core]\naskPass = /tmp/attacker-askpass\n",
        "[http]\nextraHeader = Authorization: Basic forged\n",
        "[credential]\nhelper = !/tmp/attacker-helper\n",
        '[diff "custom"]\ntextconv = /tmp/attacker-textconv\n',
        '[remote "origin"]\npushurl = https://attacker.invalid/repo.git\n',
        (
            '[branch "concurrent"]\n'
            "\tremote = origin\n"
            '\t\t[url "https://attacker.invalid/rebound.git"]\n'
            "\t\t\tinsteadOf = https://github.com/feder-positronics/intelflo.git\n"
        ),
    ],
)
def test_closeout_rejects_every_unmeasured_git_config_grammar(
    tmp_path: Path, unsafe_config: str
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        config_suffix=unsafe_config,
    )

    assert result.returncode == 2
    assert "unmeasured execution, redirect" in result.stdout


@requires_host_job_authority
def test_closeout_deletes_remote_at_reviewed_head_and_retains_local_branch(
    tmp_path: Path,
) -> None:
    reviewed_head = "b" * 40
    result = _run_outcome_admission_harness(
        tmp_path,
        remote_head_oid=reviewed_head,
        local_head_oid=reviewed_head,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert (
        "push --force-with-lease=refs/heads/feature/test:"
        f"{reviewed_head} {CANONICAL_ORIGIN} :refs/heads/feature/test"
    ) in calls
    assert "update-ref -d refs/heads/feature/test" not in calls
    assert "Local branch feature/test retained" in result.stdout


def test_closeout_refuses_remote_branch_that_moved_after_review(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        remote_head_oid="c" * 40,
    )

    assert result.returncode != 0
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "push --force-with-lease=" not in calls
    assert "update-ref -d" not in calls
    assert "moved after review" in result.stdout


@requires_host_job_authority
def test_closeout_never_deletes_unowned_local_branch_even_at_another_head(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        local_head_oid="c" * 40,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "update-ref -d refs/heads/feature/test" not in calls
    assert "Deleted local branch" not in result.stdout
    assert "Local branch feature/test retained" in result.stdout


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"issue": None}, "requires --issue"),
        ({"notes": None}, "requires --notes"),
        ({"notes": "post-merge-acceptance:$(touch-pwned)"}, "requires --notes"),
        ({"notes": "post-merge-acceptance:" + "a" * 79}, "requires --notes"),
        ({"pr_body": ""}, "requires PR body Refs #123"),
        ({"pr_body": "Refs #999"}, "requires PR body Refs #123"),
        ({"pr_body": "Closes #123"}, "requires PR body Refs #123"),
        ({"pr_body": "Fixes #123"}, "requires PR body Refs #123"),
        ({"pr_body": "Resolves #123"}, "requires PR body Refs #123"),
        ({"pr_body": "Refs #123\nCloses #123"}, "requires PR body Refs #123"),
        (
            {"pr_body": "Refs #123\nCloses owner/repo#123"},
            "requires PR body Refs #123",
        ),
        (
            {"pr_body": "Refs #123\nCloses: owner/repo#123"},
            "requires PR body Refs #123",
        ),
        ({"pr_body": "Refs #123\nFixes:#123"}, "requires PR body Refs #123"),
        (
            {"skill": "code-review", "run_owner_skill": "code-review"},
            "reserved for work-issue or execute-blueprint",
        ),
        (
            {"active_run_id": "sr_" + "b" * 32},
            "requires --run-id to identify the active outer run",
        ),
        (
            {"run_owner_skill": "execute-blueprint"},
            "must match an existing skill-owned run",
        ),
        ({"issue_state": "CLOSED"}, "requires issue #123 to be OPEN"),
    ],
)
def test_in_progress_admission_rejections_happen_before_merge(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    result = _run_outcome_admission_harness(tmp_path, **kwargs)

    assert result.returncode == 2
    assert message in result.stdout
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "pr_merge_gate.py" not in calls or "--merge" not in calls
    assert "final_ci_gate.py" not in calls
    assert not (tmp_path / "merged.marker").exists()


@requires_host_job_authority
def test_default_merged_closeout_keeps_terminal_log_capsule_and_open_warning(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(tmp_path, outcome=None)

    assert result.returncode == 0, result.stderr
    assert "still OPEN" in result.stdout
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "delivery_pipeline.py settle" in calls
    assert "--skill work-issue" in calls
    assert "reentry_capsule.py" in calls


def test_qualified_deferred_acceptance_merges_without_duplicate_log_or_capsule(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        notes="post-merge-acceptance:QA-2644",
        pr_body="Refs #123",
    )

    assert result.returncode == 0, result.stderr
    assert "retained for deferred post-merge acceptance" in result.stdout
    assert "still OPEN" not in result.stdout
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "pr_merge_gate.py" in calls
    assert "--merge" in calls
    assert "--outcome in_progress" not in calls
    assert "reentry_capsule.py" not in calls
    assert "system_error_issue_lock.py" not in calls
    assert calls.index("gh pr view") < calls.index("pr_merge_gate.py")
    assert calls.index("gh issue view") < calls.index("pr_merge_gate.py")


def test_deferred_acceptance_refuses_closed_issue_after_final_ci(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        issue_state_after_ci="CLOSED",
    )

    assert result.returncode == 2
    assert "OPEN immediately before merge" in result.stdout
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "final_ci_gate.py" in calls or "--wait" in calls
    assert "--merge" not in calls
    assert not (tmp_path / "merged.marker").exists()


def test_deferred_acceptance_supports_qualified_refs_near_ordinary_closing_words(
    tmp_path: Path,
) -> None:
    result = _run_outcome_admission_harness(
        tmp_path,
        pr_body=(
            "Refs: owner/repo#123; please fix the linked checklist before "
            "resolving the follow-up."
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "retained for deferred post-merge acceptance" in result.stdout


@requires_host_job_authority
def test_later_already_merged_default_closeout_finalizes_deferred_run(
    tmp_path: Path,
) -> None:
    first = _run_outcome_admission_harness(tmp_path)
    assert first.returncode == 0, first.stderr
    calls = tmp_path / "calls.log"
    before_finalization = calls.read_text(encoding="utf-8")

    second = _run_outcome_admission_harness(tmp_path, outcome=None)

    assert second.returncode == 0, second.stderr
    new_calls = calls.read_text(encoding="utf-8")[len(before_finalization) :]
    assert "delivery_pipeline.py settle" in new_calls
    assert "--skill work-issue" in new_calls
    assert "reentry_capsule.py" in new_calls
    assert "system_error_issue_lock.py" in new_calls
    assert "--outcome in_progress" not in new_calls


@requires_host_job_authority
def test_loopzero_closeout_runs_final_ci_merge_and_cleanup_without_phase_capsule(
    tmp_path,
):
    result = _run_outcome_admission_harness(
        tmp_path,
        outcome=None,
        issue_state="CLOSED",
        remote_head_oid="b" * 40,
        extra_env={"CLOSEOUT_DELIVERY_CONTRACT": "loop-zero-v1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert "final_ci_gate.py" in calls
    assert "pr_merge_gate.py" in calls and "--merge" in calls
    assert calls.index("final_ci_gate.py") < calls.index("delivery_pipeline.py settle")
    assert "push --force-with-lease=refs/heads/feature/test:" in calls
    assert (tmp_path / "merged.marker").exists()
    assert "--transition closeout" not in calls
    assert "reentry_capsule.py" not in calls


def test_loopzero_closeout_cannot_merge_when_required_ci_is_unavailable(tmp_path):
    result = _run_outcome_admission_harness(
        tmp_path,
        outcome=None,
        extra_env={
            "CLOSEOUT_DELIVERY_CONTRACT": "loop-zero-v1",
            "CLOSEOUT_FINAL_CI_EXIT": "1",
        },
    )
    assert result.returncode != 0
    assert not (tmp_path / "merged.marker").exists()
    calls = (tmp_path / "calls.log").read_text()
    assert "delivery_pipeline.py settle" not in calls
