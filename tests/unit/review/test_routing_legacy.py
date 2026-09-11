from pathlib import Path
from types import SimpleNamespace

import pytest

from loopzero.kernel.authority import TerminalAuthority
from loopzero.kernel.gitscope import DispatchError
from loopzero.kernel.policy import DISPATCH_POLICY_VERSION, TELEMETRY_SCHEMA_VERSION
from loopzero.review.authority import authenticated_retry_outcomes
from loopzero.review.routing import validate_retry_policy

dispatch = SimpleNamespace(
    TerminalAuthority=TerminalAuthority,
    DispatchError=DispatchError,
    DISPATCH_POLICY_VERSION=DISPATCH_POLICY_VERSION,
    TELEMETRY_SCHEMA_VERSION=TELEMETRY_SCHEMA_VERSION,
    authenticated_retry_outcomes=authenticated_retry_outcomes,
    validate_retry_policy=validate_retry_policy,
)


@pytest.fixture(autouse=True)
def protected_openssl(
    monkeypatch: pytest.MonkeyPatch,
    isolated_ptrace_scope_path: Path,
) -> None:
    authority_globals = dispatch.TerminalAuthority.__init__.__globals__
    monkeypatch.setitem(authority_globals, "_openssl", lambda: Path("/usr/bin/openssl"))
    monkeypatch.setitem(
        authority_globals, "PTRACE_SCOPE_PATH", isolated_ptrace_scope_path
    )


def governed(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Stamp fixture records with the active dispatcher contract."""
    return [
        {
            **record,
            "schema_version": dispatch.TELEMETRY_SCHEMA_VERSION,
            "policy_version": dispatch.DISPATCH_POLICY_VERSION,
        }
        for record in records
    ]


def failed_authenticated_grok_review() -> list[dict[str, object]]:
    signer = dispatch.TerminalAuthority.generate()
    start, terminal = governed(
        [
            {
                "type": "attempt-start",
                "task_id": "grok-review",
                "work_unit_id": "review-unit",
                "attempt_index": 0,
                "unit_attempt_number": 1,
                "run_id": "sr_" + "a" * 32,
                "worktree": "/tmp/grok-review",
                "terminal_authority": signer.registration(),
            },
            {
                "type": "attempt-terminal",
                "task_id": "grok-review",
                "work_unit_id": "review-unit",
                "attempt_index": 0,
                "unit_attempt_number": 1,
                "run_id": "sr_" + "a" * 32,
                "worktree": "/tmp/grok-review",
                "alias": "grok-cursor",
                "effective_alias": "grok-cursor",
                "effort": "high",
                "status": "failed",
                "failure_class": "model-result",
                "work_kind": "review",
                "read_only": True,
            },
        ]
    )
    terminal = signer.seal(terminal, authority_kind="dispatcher")
    records = [start, terminal]

    assert dispatch.authenticated_retry_outcomes(records) == [terminal]
    return records


def test_failed_authenticated_grok_review_escalates_to_sol_high() -> None:
    records = failed_authenticated_grok_review()

    assert (
        dispatch.validate_retry_policy(
            records,
            task_id="sol-review",
            work_unit_id="review-unit",
            alias="sol",
            effort="high",
        )
        == 2
    )


@pytest.mark.parametrize("alias", ["grok-cursor", "opus", "terra"])
def test_failed_authenticated_grok_review_rejects_other_routes(alias: str) -> None:
    records = failed_authenticated_grok_review()

    with pytest.raises(
        dispatch.DispatchError,
        match="must escalate from grok-cursor",
    ):
        dispatch.validate_retry_policy(
            records,
            task_id=f"{alias}-review",
            work_unit_id="review-unit",
            alias=alias,
            effort="high",
        )
