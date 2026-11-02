# A readable backlog

A convention for repositories that use loop-zero, so a human owner can see in
ten seconds how much work is left and what comes next. It changes no command,
config or state: it is a way to label issues and keep one summary.

## For the owner

Every open issue is one of three kinds:

- **Problem:** something is broken, or a required proof is still missing.
- **Goal:** a defined thing to build or decide, with a clear finish.
- **Ongoing duty:** a standing responsibility that never finishes, such as a
  recurring check. Duties are listed but never counted as work.

**One number: to do = problems + goals.** Held, blocked and waiting work still
counts, and so does anything not yet labelled. Calling something a duty never
hides finite work.

One pinned **"Work still to do"** issue shows that number, the split into
problems and goals, the duties underneath, links to the live issue lists, and
the opened and closed totals for the last seven days. It shows when it was last
refreshed; if a refresh fails, it says so and keeps the last dated view rather
than showing stale numbers as current.

**"Next up"** lists at most about eight verified, unheld picks for the next
batch, with the full count alongside. A short next-up list is focus, not a
limit on the backlog.

## For agents

- **Create:** give each issue exactly one kind. Use a plain title and state the
  outcome and the evidence that closes it. Before opening one, look for an open
  issue in the same failure family and add to it instead.
- **Automated reporters** reuse one open issue per failure family. Repeated runs
  update its latest observation; distinct failures, especially security or
  privacy defects, get their own issue. A confirmed recurrence of a closed one
  reopens it under the same recovery rule.
- **Work:** ownership, claims, linked PRs and acceptance work as before. A hold
  is a line in the live decision record ([DELEGATION.md](DELEGATION.md)),
  linked from the issue, never a label or an inference from age.
- **Close:** closure rules do not change. Incidents, QA issues and goals close
  on their own acceptance and authority ([CONTRACT.md](../core/CONTRACT.md),
  [DELEGATION.md](DELEGATION.md)). A green unrelated job, age or a count target
  is not closure evidence.
- **PR review findings** stay in the PR's review threads. Never copy them into
  backlog issues.
- **Summary:** the existing delivery sweep refreshes the pinned summary through
  the REST API and reports any unlabelled or conflicting issues. Add no gate,
  ledger or second work state.
- **Counting:** when asked for a count, state the repository's counting rule
  and place every open issue as counted, ongoing duty or unclassified (see the
  `backlog` skill).

## Consumer choices

Each repository names its three labels and the script or sweep step that
writes the summary in its own `AGENTS.md`. loop-zero ships no labels and no
summary script. IntelFlo is the worked example: `problem`, `goal` and `monitor`
labels, a pinned "Work still to do" issue refreshed by the delivery
coordinator's sweep, and its existing `NEXT` label for next-up picks.
