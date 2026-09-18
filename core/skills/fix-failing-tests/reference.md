# Fix Failing Tests Reference

Use this reference when choosing validation commands.

Run the narrowest documented repository command that reproduces the failure.
Do not pipe it through output filters that hide the runner's exit status. After
the edit, rerun that exact command first, then expand to the owning package or
neighboring integration boundary.

Do not install or update shared dependencies merely because a tool is missing.
Use the repository's documented setup and frozen-lockfile path only when the
dependency state is demonstrated to be stale. Treat runner loss, missing tools,
network failure, and sandbox failure as infrastructure evidence rather than a
reason to alter the product or test.
