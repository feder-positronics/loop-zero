# Reviewing T3 Code and Orchestrator updates

Keep loop-zero's use of T3 current without turning upstream release notes into
delivery policy. T3 owns execution, workspaces, notifications and interaction;
loop-zero retains its [delivery contract](../core/CONTRACT.md). The dated
[workflow exploration](design/2026-10-28-t3-orchestration-workflow-adaptations.md)
records the initial research and proposed adaptations, not shipped features.

## Owner and cadence

The repository's delivery coordinator owns this review. Without a coordinator,
the maintainer responsible for the repository's T3 integration owns it.

- During active T3 use, review developments once a week as part of existing
  maintenance work. When inactive, review before the next T3-dependent task.
- Review before a T3 or provider-harness upgrade, adoption of an orchestration
  capability, or repair of a compatibility, delegation, wakeup or workspace fault.
- This is a reading and triage routine. It creates no timer, service, reminder
  issue or new scheduler. An unattended recurring job requires a separate request.

## Sources to read

Start with the running environment's version, exposed tool schemas and injected
instructions. Check the relevant provider/model capabilities when required by
those instructions; do not add repeated discovery calls to every task.

Use the official [T3 Code releases](https://github.com/pingdotgg/t3code/releases)
to distinguish stable releases from nightlies. Inspect relevant changes to
delegation, completion, approvals/questions, model options, context transfer,
workspace isolation, PR watches, remote access and recovery. Follow a linked PR
when its change affects our integration; a proposal or merge into upstream main
does not establish deployment in the running environment.

The primary upstream references are:

- [Agent orchestration instructions](https://github.com/pingdotgg/t3code/blob/main/packages/provider-core/src/server/orchestrationInstructions.ts):
  compact usage guidance supplied to agents.
- [MCP guide](https://github.com/pingdotgg/t3code/blob/main/docs/orchestration-v2/orchestrator-mcp-server.md),
  [tool definitions](https://github.com/pingdotgg/t3code/blob/main/apps/server/src/mcp/toolkits/orchestrator/tools.ts)
  and [API contracts](https://github.com/pingdotgg/t3code/blob/main/packages/contracts/src/orchestratorMcp.ts):
  task/thread distinction, parameters, options, results and completion semantics.
- [V2 documentation index](https://github.com/pingdotgg/t3code/tree/main/docs/orchestration-v2),
  especially feature lifecycles, lineage/context transfer and provider
  capabilities. Respect sections labelled target behavior or proposed work.
- [User documentation](https://github.com/pingdotgg/t3code/tree/main/docs/user):
  provider setup, outside agents, remote environments and thread behavior.
- [Theo's published skills](https://github.com/t3dotgg/tstack): examples of task
  wording and composition; they do not define our permissions or delivery gates.

These discovery links track upstream. For a technical conclusion, resolve them
to the installed release or source revision and retain that immutable reference
in the relevant existing issue, PR or dated research note. Do not silently treat
the latest upstream instruction text as the installed app's instruction set.

## Review and adoption procedure

1. Identify the environment and installed T3/provider versions when observable;
   otherwise say unknown. Read the applicable live tools/instructions and compare
   with source at that version. Reconcile differing examples against the actual
   schema and implementation; documentation can lag code.
2. Compare relevant upstream changes since that baseline. Classify each as
   installed and verified, released but not installed, upstream-only, proposed,
   or unknown. A changelog claim alone is not a successful compatibility probe.
3. Ask whether the change removes an existing workaround, repairs an observed
   failure, or enables an already-needed workflow. Keep runtime mechanics in T3,
   host policy on the host, consumer acceptance in the consumer, and delivery
   evidence in Git/GitHub/CI. Prefer deleting obsolete choreography to adding it.
4. Before adopting a relevant change, exercise the smallest authorized bounded
   case on a disposable task or fixture. Record the version/revision, expected
   result, observed result and limitations on its existing delivery record.
   Include failure/re-entry behavior when that is the changed capability. A
   read-only brief is not an OS sandbox, and a pending approval grants no new
   approval authority.
5. Deliver justified loop-zero changes through the ordinary contract and
   [architecture review](ARCHITECTURE.md#keeping-the-guide-current). Reading
   upstream authorizes neither upgrades nor changes to credentials, production,
   sandboxing, model routing, review budgets or merge policy. Active user and
   session instructions continue to govern execution.

## Report only actionable differences

Use the owning issue/PR or a necessary dated design note, not a parallel ledger.
For a material difference, name its evidence, affected behavior, proposed action,
next-action owner and compatibility test. Keep documentation changes proportional
to the result. Unchanged evidence needs no broadcast, timestamp-only commit or
new task. If a comparison cannot be verified, identify the unknown and defer only
the affected adoption; continue independent authorized work.
