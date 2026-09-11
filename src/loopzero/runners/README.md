# Runtime adapters (cutover A)

The Claude, Codex and Cursor transports retain intelflo's parsing, fallback,
credential, process ownership and error behavior. SDK imports are lazy; the
base package requires only the standard library. Credential and token helpers
are merged into their vendor module; names and call signatures are unchanged,
and importing callers use the new module path.
`contract.py` includes the names formerly in `contracts.py` and
`governed_result.py`; `_review_schema.py` holds their unchanged schema helpers.

Configure a consumer and a filesystem sandbox before constructing adapters or
calling credential helpers. Default adapter construction is useful for parsing
and dependency injection, but it is not an executable containment boundary:

```python
from functools import partial

from loopzero.runners.containment import worker_isolated_command
from loopzero.runners.process import isolated_python_import_available, run_cli
from loopzero.runners.settings import RuntimeSettings
from loopzero.runners.registry import NATIVE_RUNTIME_REGISTRY

settings = RuntimeSettings.from_profile(profile)
sandbox = partial(
    worker_isolated_command,
    writable_root=worktree,
    readable_roots=(settings.tooling_root,),
)
contained_run = partial(run_cli, sandbox_wrapper=sandbox)
contained_probe = lambda python, cwd: isolated_python_import_available(
    python, "claude_agent_sdk", timeout_s=5,
    sandbox_wrapper=sandbox,
)
with settings.use():
    adapter = NATIVE_RUNTIME_REGISTRY.create(
        "claude", run_cli=contained_run, run_probe=contained_run,
        sdk_available=contained_probe,
    )
    # Credential helper calls also belong inside this scope.
result = adapter.run(request)  # retains the settings used at construction
```

`launch_cli()` and `run_cli()` refuse execution without a caller-supplied
`sandbox_wrapper`. The exceptional `unsandboxed=True` path also requires a
nonempty reason and logs it. It is intended for tightly scoped process-mechanics
tests, not validation children. The built-in `worker_isolated_command()` is the
ported bubblewrap argv builder: it exposes the selected writable root while
remounting its `.git` metadata read-only, and exposes only explicit read roots.
`worker_child_environment()` starts from the settings allowlist and always drops
GitHub tokens, SSH agent access, and every lease/nonce-named variable.

Existing helper signatures are unchanged. Settings scopes are context-local,
restore their parent on exit, and do not mutate module globals. Exported string
constants describe the default namespace; consumers sharing environment names
with their parent should use `settings.env_name("CLAUDE_AUTH_FD")`, etc.
`RuntimeSettings(env_prefix="INTELFLO")` reproduces legacy environment, temporary,
lock and token-state names. Set `toolchain_interpreter` to
`Path("fastapi_backend/.venv/bin/python")` for intelflo's existing SDK environment;
`from_profile` reads that from `[toolchain].interpreter`. The profile root also
anchors the canonical evidence path, which cannot be anchored at site-packages.

During cutover A, **intelflo should use the absolute packaged bridge file**:
`<toolchain-python> -I <absolute-package-path>/runners/bridge.py` (the adapters
build this command). This works when its SDK venv does not have loopzero
installed. File execution establishes a private package path rooted at the
bridge's own directory; it does not change `sys.path` or load siblings by file
specs. `python -m loopzero.runners.bridge` also works when loopzero is installed.
The internal `LOOPZERO_RUNTIME_SETTINGS` bootstrap carries only injected names
and paths to the bridge and is consumed before the SDK runs. Codex refresh uses
this same bridge with `--codex-refresh`; `codex_refresh()` imports openai_codex
only when invoked. The private SDK shims remain intact.

Run the suite with `uv sync --group dev` followed by
`.venv/bin/python -m pytest -q`. The dev group pins `claude-agent-sdk==0.2.152`
and `openai-codex==0.147.0`, so the original SDK assertions are unconditional.

`RUNTIME_REGISTRY` adds the deterministic `fake` runner; the legacy
`NATIVE_RUNTIME_REGISTRY` still lists only Claude, Codex and Cursor. A fake
`ScenarioSpec` accepts all seven cutover scenarios. A restart checkpoint moves
into a newly constructed fake adapter and preserves progress sequence numbers.
`LOOPZERO_CONFORMANCE_RUNNER=claude|codex|cursor` selects the native adapter
**with replayed process responses**. These tests exercise normalized protocol
faults without credentials or paid turns. Live SDK/session conformance is not
claimed by this suite.
