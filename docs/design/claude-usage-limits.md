# Claude structured usage-limit observations

The Claude SDK bridge reports `limited` / `usage-limit` when an unsuccessful
native result follows an assistant `rate_limit` error, or a rejected known
provider window plus HTTP 429. A generic 429 or matching error text alone does
not establish a subscription-window limit. Advisory events alone cannot turn a
successful completion into a limit; a previously accepted StructuredOutput tool
result survives a later limit terminal.

`RuntimeResult.usage_limit` contains only a closed window scope and optional
bounded Unix reset timestamp. Missing, malformed, boolean, nonfinite, nonpositive,
or out-of-range reset values remain unknown (`None`). An assistant rate-limit
error without a known rejected window has unknown scope and reset. This is an
observation, not proof that every credential for an account is exhausted.

Commercial-boundary checks retain precedence. Permitted overage clears the
rejected base-window observation, and local budget exhaustion remains a distinct
terminal. Partial output is retained when model activity was observed. Provider
attempt and terminal evidence still set the existing semantic marker that prevents
fallback; this marker is not a claim that model output occurred.

This bounded #32 follow-up adds no retry, account rotation, cooldown, selection,
quota settlement, or Codex classification. Native-object tests use the pinned SDK
schema without live vendor requests. End-to-end consumer scheduling and durable
account evidence remain separate work.
