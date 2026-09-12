# Runtime adapters (cutover A)

The Claude, Codex and Cursor transports retain intelflo's parsing, fallback,
credential, process ownership and error behavior. SDK imports are lazy; the
base package requires only the standard library. Credential and token helpers
are merged into their vendor module; names and call signatures are unchanged,
and importing callers use the new module path.
`contract.py` includes the names formerly in `contracts.py` and
`governed_result.py`; `_review_schema.py` holds their unchanged schema helpers.

Configure a consumer and obtain a filesystem wrapper from the kernel before
constructing adapters or calling credential helpers. Default adapter
construction is useful for parsing and dependency injection, but it is not an
executable containment boundary:

```python
from loopzero.runners.process import LaunchSpec, isolated_python_import_available, run_cli
from loopzero.runners.settings import RuntimeSettings
from loopzero.runners.registry import NATIVE_RUNTIME_REGISTRY

settings = RuntimeSettings.from_profile(profile)
workspace_root = settings.workspace_root(worktree)
workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
# The dispatcher supplies this callable after asking loopzero.kernel.sandbox
# to settle filesystem authority. Each call receives only that launch's paths.
sandbox_wrapper = dispatcher_sandbox_wrapper
contained_run = lambda argv, **kwargs: run_cli(
    argv, sandbox_wrapper=sandbox_wrapper, **kwargs
)
contained_probe = lambda python, cwd: isolated_python_import_available(
    python, "claude_agent_sdk", timeout_s=5,
    sandbox_wrapper=sandbox_wrapper,
)
with settings.use():
    adapter = NATIVE_RUNTIME_REGISTRY.create(
        "claude", run_cli=contained_run, run_probe=contained_run,
        sdk_available=contained_probe,
    )
    # Credential helpers receive the same wrapper through their sandbox_wrapper
    # argument; they never construct a filesystem sandbox themselves.
result = adapter.run(request)  # retains the settings used at construction
```

`launch_cli()` and `run_cli()` refuse execution without a caller-supplied
`sandbox_wrapper`. The exceptional `unsandboxed=True` path also requires a
nonempty reason and logs it. It is intended for tightly scoped process-mechanics
tests, not validation children. The wrapper contract is
``LaunchSpec -> argv`` and is owned by `loopzero.kernel.sandbox`; the dispatcher
wires it into runners. `LaunchSpec` contains the original `argv`, `cwd`, exact
per-launch `private_mounts`, and a fresh 0700 `private_tmpdir`. The wrapper must
bind the governed worktree, tooling root, runtime venv root, and
`settings.workspace_root(tooling_root)`, then writable-bind only the paths in
that launch's `private_mounts`. It must writable-bind `private_tmpdir` and the
runner exports that exact path as `TMPDIR`; a private tmpfs mounted there is an
equivalent implementation. This is required because bridge credential and
configuration bootstraps create temporary files. The wrapper must also provide
a private `/dev` and `/proc` to the child (Python refuses to start without
`/dev/urandom`). The venv binding must include the
interpreter's `pyvenv.cfg`, `bin`, and `lib` so the bridge can import its SDKs.
Every runner child cwd is inside either the governed worktree or that settings
workspace root, except credential renewal children. Renewals use a random,
atomically created 0700 directory below the consumer state root, pass only that
directory in the refresh launch's `private_mounts`, and scrub it afterwards.
The wrapper must never bind the state root itself or carry private mounts from
one launch into another; ordinary workers therefore cannot see refresh
material. Codex transfers the existing refresh-capable credential to its bridge
on the original sealed-descriptor boundary and writes only the rotated SDK
output into staging. The Claude CLI has no descriptor credential input, so its
input file remains confined to the same launch-private directory.
Every `.git` path and every linked-worktree gitdir reachable through those
bindings must remain read-only. Runners neither build bubblewrap argv nor decide
which roots are writable. `worker_child_environment()` starts from the positive
settings allowlist and always drops GitHub tokens, SSH agent access, and every
lease/nonce-named variable.

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
