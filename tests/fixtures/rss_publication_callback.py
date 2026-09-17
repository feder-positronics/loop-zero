# Exact publication_request function from IntelFlo cac7972f1 scripts/util/pr_publish.py.
# Bootstrap and host imports outside this function are deliberately excluded.
# Tests supply an isolated host and package PublicationRequest/PublicationError.
def publication_request(*, authority_repo, run_id, **kwargs):
    """Bind provisional ownership only after the publisher validates a real PR."""
    from loopzero.delivery._publish_findings import resolved_loopzero_predecessors
    from loopzero.review import provisional_findings
    import agent_dispatch as host

    review_task_id = kwargs.get("review_task_id")

    def bind(pr, head, base, repository):
        if head != kwargs["expected_head"] or base != kwargs["base"]:
            raise PublicationError("provisional publication source changed")
        with host.authority_ledger_lock(authority_repo):
            records = host.load_authority_records(
                authority_repo, host.WORK_UNIT_HISTORY_DAYS
            )
            terminals = host.authenticated_review_terminals(records)
            selected = resolved_loopzero_predecessors(
                authority_repo,
                terminals=terminals,
                current_review_task_id=review_task_id,
                verdicts=host.authenticated_verdicts(records),
            ) | {review_task_id}
            admissions = provisional_findings.authenticated_provisional_admissions(
                records
            )
            bindings = provisional_findings.authenticated_publication_bindings(records)
            pending = []
            for task_id in sorted(task for task in selected if task):
                admission = admissions.get(task_id)
                if admission is None:
                    continue
                if (
                    admission.get("run_id") != run_id
                    or admission.get("source_identity", {}).get("ref")
                    != f"refs/heads/{kwargs['head']}"
                ):
                    raise PublicationError("provisional publication owner changed")
                terminal = terminals.get(task_id, {})
                payload = provisional_findings.build_publication_binding(
                    records,
                    repo=authority_repo,
                    admission=admission,
                    capture_receipt=terminal.get("finding_capture_receipt", {}),
                    pr=pr,
                    head=head,
                    base=base,
                    repository=repository,
                )
                pending.append(
                    (payload, terminal["finding_capture_receipt"]["finding_count"])
                )
            # Validate every owner before appending any binding. A failed
            # materialization retains its binding for the same-PR retry.
            for payload, finding_count in pending:
                owner = payload["provisional_owner_id"]
                if owner not in bindings:
                    sealed = host.create_coordinator_authority().seal(
                        host._record_envelope(payload),
                        authority_kind="coordinator",
                        include_public_key=True,
                    )
                    host._append_serialized_record_unlocked(authority_repo, sealed)
                records = host.load_authority_records(
                    authority_repo, host.WORK_UNIT_HISTORY_DAYS
                )
                binding = provisional_findings.authenticated_publication_bindings(
                    records
                )[owner]
                # The package intentionally excludes suggestions and refuses
                # empty durable capture batches. Their signed binding remains.
                if finding_count:
                    provisional_findings.materialize_publication_binding(
                        authority_repo, authority_records=records, binding=binding
                    )

    return PublicationRequest(**kwargs, bind_provisional_findings=bind)
