# Runtime adapters (cutover A)

The Claude, Codex and Cursor transports retain intelflo's parsing, fallback,
credential, process ownership and error behavior. SDK imports are lazy; the
base package requires only the standard library. Credential helpers stay in
separate sibling modules to preserve their existing call and monkeypatch seams.
`contract.py` includes the names formerly in `contracts.py` and
`governed_result.py`; `_review_schema.py` holds their unchanged schema helpers.

Configure a consumer before constructing adapters or calling credential helpers:

```python
from loopzero.runners.settings import RuntimeSettings
from loopzero.runners.registry import NATIVE_RUNTIME_REGISTRY

settings = RuntimeSettings.from_profile(profile)
with settings.use():
    adapter = NATIVE_RUNTIME_REGISTRY.create("claude")
    # Credential helper calls also belong inside this scope.
result = adapter.run(request)  # retains the settings used at construction
```

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

Run the portable suite with `python -m pytest -q`. SDK-dependent tests use
`pytest.importorskip` when the optional SDK is absent. To run the original SDK
integration assertions as well, install `claude-agent-sdk==0.2.152` and
`openai-codex==0.147.0` into the test interpreter. The latter is the source
bridge's pinned version, enforced without changing its compatibility check.

`RUNTIME_REGISTRY` adds the deterministic `fake` runner; the legacy
`NATIVE_RUNTIME_REGISTRY` still lists only Claude, Codex and Cursor. A fake
`ScenarioSpec` accepts all seven cutover scenarios. A restart checkpoint moves
into a newly constructed fake adapter and preserves progress sequence numbers.
`LOOPZERO_CONFORMANCE_RUNNER=claude|codex|cursor` selects the native adapter
**with replayed process responses**. These tests exercise normalized protocol
faults without credentials or paid turns. Native cancellation remains a
transport disconnect (Cursor reports a malformed incomplete stream), and the
old request contract has no resume token: native restart is an explicit new
invocation. Live SDK/session conformance is not claimed by this suite.
