"""Observed remote identities and exact-ref leased publication convergence."""

import re
from pathlib import Path

from ._publish_equivalence import prove_republish_equivalence
from ._publish_paths import PublicationError
from ._publish_risk import CommandRunner


def _remote_branch_head(
    runner: CommandRunner,
    *,
    remote_ref: str,
) -> str | None:
    completed = runner.run(
        ["git", "ls-remote", "--heads", "origin", remote_ref],
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) != 1:
        raise PublicationError("remote branch lookup returned ambiguous results")
    fields = lines[0].split()
    if (
        len(fields) != 2
        or re.fullmatch(r"[0-9a-f]{40}", fields[0]) is None
        or fields[1] != remote_ref
    ):
        raise PublicationError("remote branch lookup returned an invalid result")
    return fields[0]


def trusted_publication_base_head(
    runner: CommandRunner,
    *,
    base: str,
) -> str:
    """Bind conservative path scans to the observed remote base head."""

    branch_check = runner.run(
        ["git", "check-ref-format", "--branch", base],
        check=False,
    )
    if branch_check.returncode != 0:
        raise PublicationError("publication base is not a safe branch name")
    remote_head = _remote_branch_head(
        runner,
        remote_ref=f"refs/heads/{base}",
    )
    if remote_head is None:
        raise PublicationError("publication base does not exist on origin")
    local_tracking = runner.run(
        ["git", "rev-parse", "--verify", f"refs/remotes/origin/{base}"],
        check=False,
    )
    if local_tracking.returncode != 0 or local_tracking.stdout.strip() != remote_head:
        raise PublicationError(
            "origin base tracking ref is stale; fetch the pinned base before "
            "publication"
        )
    return remote_head


def converge_remote_publication_head(
    runner: CommandRunner,
    *,
    head: str,
    expected_head: str,
    repo: Path | None = None,
    base_head: str | None = None,
    base: str = "main",
    equivalence_evidence: dict[str, object] | None = None,
) -> str:
    """Converge one PR branch under its exact observed remote-head lease."""
    if head in {"main", "master", base} or head.startswith("refs/"):
        raise PublicationError("publication head is a protected or qualified branch")
    if re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
        raise PublicationError("expected head must be exactly 40 lowercase hex")
    branch_check = runner.run(
        ["git", "check-ref-format", "--branch", head],
        check=False,
    )
    if branch_check.returncode != 0:
        raise PublicationError("publication head is not a safe branch name")
    remote_ref = f"refs/heads/{head}"
    observed = _remote_branch_head(runner, remote_ref=remote_ref)
    proof = None
    if observed == expected_head:
        return expected_head
    if observed is not None:
        # The shared secure runner disables replacement objects for this local
        # identity proof; the following lease is safe only when this is real
        # object ancestry rather than a refs/replace view.
        ancestry = runner.run(
            ["git", "merge-base", "--is-ancestor", observed, expected_head],
            check=False,
        )
        if ancestry.returncode != 0:
            if ancestry.returncode == 1 and repo is not None and base_head is not None:
                proof = prove_republish_equivalence(
                    repo=repo,
                    observed_head=observed,
                    expected_head=expected_head,
                    base_head=base_head,
                )
            if proof is None:
                raise PublicationError(
                    "remote branch head is not an ancestor of the reviewed head; "
                    "republish from the exact reviewed tree instead of re-merging "
                    "a stale head"
                )

    lease = observed or ""
    pushed = runner.run(
        [
            "git",
            "push",
            "--porcelain",
            f"--force-with-lease={remote_ref}:{lease}",
            "origin",
            f"{expected_head}:{remote_ref}",
        ],
        check=False,
    )
    if pushed.returncode != 0:
        raise PublicationError(
            "remote branch changed during publication or rejected the exact-head "
            "push; rerun publication from the reviewed local head"
        )
    verified = _remote_branch_head(runner, remote_ref=remote_ref)
    if verified != expected_head:
        raise PublicationError("remote branch did not converge to the expected head")
    if proof is not None and equivalence_evidence is not None:
        equivalence_evidence.update(proof)
    return verified
