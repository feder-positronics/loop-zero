# Claude structured usage-limit observations

The Claude SDK bridge reports `limited` / `usage-limit` when an unsuccessful
native result has terminal HTTP 429 plus a current assistant `rate_limit` error
or rejected known provider window. Only `success` (the SDK's documented API-error
subtype) and `error_during_execution` are supported; local, cancellation, unknown
terminal causes and missing HTTP status remain failed. A later assistant message
supersedes the prior assistant error. A generic 429 or matching error text alone does
not establish a subscription-window limit. Advisory events alone cannot turn a
successful completion into a limit; a previously accepted StructuredOutput tool
result survives a correlated later limit terminal as `completed` with the explicit
`usage-limit-after-result` reason and `accepted-tool-result` recovery marker.
The parser requires this marker, bounded structured output, terminal HTTP 429,
and a supported subtype; other status/reason combinations fail closed.

`RuntimeResult.usage_limit` contains only a closed window scope and optional
bounded Unix reset timestamp. Missing, malformed, boolean, nonfinite, nonpositive,
or out-of-range reset values remain unknown (`None`). An assistant rate-limit
error without a known rejected window has unknown scope and reset. This is an
observation, not proof that every credential for an account is exhausted.
Recovered completed results retain their qualified reason, but not the window
scope/reset payload: `usage_limit` remains restricted to `limited` terminals.

Commercial-boundary checks retain precedence. Permitted overage clears the
rejected base-window observation, and local budget exhaustion remains a distinct
terminal. Partial output is retained when model activity was observed. Provider
attempt and terminal evidence still set the existing semantic marker that prevents
fallback; this marker is not a claim that model output occurred.

This bounded #32 follow-up adds no retry, account rotation, cooldown, selection,
quota settlement, or Codex classification. Native-object tests use the pinned SDK
schema without live vendor requests. End-to-end consumer scheduling and durable
account evidence remain separate work.
