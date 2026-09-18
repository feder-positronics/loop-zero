# Run reviewers inside bwrap

Date: 2026-09-18. Status: proposed.

## Failure today

`runners._review_claude` restricts the reviewer with CLI flags (plan mode,
read-only tools, no settings, no MCP). `runners._review_codex` uses the
vendor's read-only sandbox and a throwaway `CODEX_HOME`. Both processes still
run on the host with the caller's filesystem view. A reviewer that follows a
prompt injection in the diff can read `~/.ssh`, other tokens, or unrelated
repositories. Network is required for review, so this change bounds what the
reviewer can *read*, not what it can send; that is stated plainly.

## Change

- `sandbox.bwrap_argv` is reused with a reviewer-specific `Config`, built in
  `runners`, not inherited from `[checks]`:
  - network on;
  - worktree and Git directories read-only (existing behaviour);
  - `ro_paths` limited to the reviewer binary's install prefix and its
    runtime (for `claude`, the npm or bun prefix; for `codex`, its binary);
    read from a new `[delivery].reviewer_ro_paths` list, defaulting to the
    resolved directory of `shutil.which(family)`;
  - `writable` empty; `env` empty; `env_allowlist` is the reviewer's auth
    variables plus `PATH LANG LC_ALL TERM`.
- The private sandbox HOME receives only the reviewer's auth file, copied
  in (`~/.claude/.credentials.json` or the existing Codex `auth.json` copy).
  A CLI that refreshes tokens writes into the sandbox HOME, which is
  discarded; the host credential is never touched.
- `_review_codex` moves its schema and last-message files into the sandbox
  HOME, because `/tmp` is a fresh tmpfs inside bwrap.
- `SandboxUnavailable` from the probe fails the review with the same
  message `check` prints. Reviews never fall back to unsandboxed execution.

## Trade-off

The reviewer loses access to tools installed under the caller's home unless
they are listed. That is the point. The install prefix default covers the
common case; consumers with unusual layouts add a path.

## Tests

- Fake `bwrap` fixture records argv and executes the tail so the fake
  `claude`/`codex` still answer; assert the worktree is bound read-only,
  `--share-net` is present, and no path outside the allowlist is bound.
- Codex schema and output files are inside the bound HOME.
- Missing `bwrap` fails `review` with `SandboxUnavailable`.
- Auth variables are forwarded; unrelated host variables are not.

## Cost

80–150 lines in `runners.py`, `config.py` and tests. This is the largest item
and the only one that touches every reviewer test.
