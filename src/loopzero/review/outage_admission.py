"""Trusted consumer transaction for the one authorized outage replacement.

The callbacks are host adapters, never model inputs. Execution admission must
run the consumer's ordinary circuit, capability and remaining-budget checks for
the exact named route without fallback. It may reserve budget but must not launch.
Storage must sign, append and fsync exactly the supplied rows before returning.
"""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ..kernel import authority_store, worktree_lease
from ..kernel.gitscope import DispatchError, ReviewSnapshot
from . import admission
from .outage_recovery import validate_outage_recovery_start


def admit_outage_review(
    repository: Path,
    *,
    worktree: Path,
    task: Mapping[str, object],
    expected_source: Mapping[str, object],
    snapshot: ReviewSnapshot,
    authorization_sha256: str,
    route: Mapping[str, object],
    attempt_index: int,
    load_records: Callable[[], Sequence[dict[str, object]]],
    append_records: Callable[[Sequence[Mapping[str, object]]], None],
    admit_execution: Callable[
        [Sequence[dict[str, object]], Mapping[str, object], Mapping[str, object]], None
    ],
    required_sections: tuple[str, ...],
) -> admission.Admission:
    """Persist exactly one reservation; interrupted reservation is never replayed.

    The caller still registers its dispatcher start atomically through
    ``validate_outage_recovery_start`` immediately before launch, reapplying normal
    execution gates. This function provides no permission to reuse a prior result.
    """
    with (
        worktree_lease.worktree_lease(
            worktree, boundary="review-outage-recovery", timeout_s=0
        ),
        authority_store.authority_ledger_lock(repository),
    ):
        try:
            if (
                not isinstance(authorization_sha256, str)
                or authority_store.authority_repository_binding(worktree)
                != authority_store.authority_repository_binding(repository)
                or worktree_lease.source_identity(worktree) != dict(expected_source)
            ):
                raise DispatchError("OutageSourceBindingError")
            scoped_task = {**task, "idempotency_key": "outage:" + authorization_sha256}

            def resolve(records):
                return admission.admit_review(
                    repository,
                    records,
                    repository_binding=authority_store.authority_repository_binding(
                        repository
                    ),
                    task=scoped_task,
                    current_source_identity=expected_source,
                    current_tree_sha=snapshot.tree_sha,
                    patch_identity=snapshot.patch_identity,
                    required_sections=required_sections,
                    equivalence_proof=None,
                    format_only_proof=None,
                    requested="delta" if task.get("delta_from_snapshot") else "review",
                    changed_paths=None,
                    security_trigger_paths=(),
                    attempt_index=attempt_index,
                    source_worktree=worktree,
                    outage_authorization_sha256=authorization_sha256,
                    replacement_route=route,
                )

            records = load_records()
            result = resolve(records)
            if not isinstance(result, admission.Reserved) or result.slot.existing:
                return (
                    result
                    if isinstance(result, admission.Blocked)
                    else admission.Blocked(
                        "missing-evidence", {"cause_class": "OutageReservationRefused"}
                    )
                )
            admit_execution(records, result.scoped_task, route)
            # A host budget adapter can append accounting evidence. Reload it;
            # never discard those rows or authenticate a caller-built list.
            result = resolve(load_records())
            if not isinstance(result, admission.Reserved) or result.slot.existing:
                return admission.Blocked(
                    "missing-evidence", {"cause_class": "OutageReservationRefused"}
                )
            append_records(result.records_to_append)
            validate_outage_recovery_start(
                repository,
                load_records(),
                reservation_id=result.slot.reservation_id,
                authorization_sha256=authorization_sha256,
                task=scoped_task,
                source_identity=expected_source,
                run_id=task.get("run_id"),
                generation_id=result.generation.generation_id,
                family=result.slot.family,
                slot_kind=result.slot.slot_kind,
                route=route,
                attempt_index=attempt_index,
            )
            return result
        except (DispatchError, ValueError, OSError) as exc:
            # No refund, cleanup or second attempt after any partial write.
            return admission.Blocked(
                "missing-evidence", {"cause_class": type(exc).__name__}
            )
