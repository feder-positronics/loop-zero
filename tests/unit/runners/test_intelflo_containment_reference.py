"""Optional comparison against the pre-extraction IntelFlo argv builder."""

import importlib.util
import json
from pathlib import Path

import pytest


INTELFLO_SOURCE = Path("/home/marcin/dev/intelflo/scripts/util/agent_dispatch.py")
GOLDEN = Path(__file__).parent / "fixtures/intelflo_worker_argv.json"


def test_intelflo_argv_golden_has_fixed_credential_binding() -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert expected[:2] == ["/usr/bin/bwrap", "--die-with-parent"]
    assert expected[-7:] == [
        "--perms", "0600", "--ro-bind-data", "37",
        "/fixture/worktree/.credentials/auth.json", "--", "runtime",
    ]


@pytest.mark.skipif(
    not INTELFLO_SOURCE.is_file(),
    reason=(
        "IntelFlo argv reference unavailable: "
        "/home/marcin/dev/intelflo/scripts/util/agent_dispatch.py is absent"
    ),
)
def test_intelflo_worker_argv_matches_checked_in_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("_intelflo_argv_reference", INTELFLO_SOURCE)
    assert spec is not None and spec.loader is not None
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(original, "system_executable", lambda _: Path("/usr/bin/bwrap"))
    monkeypatch.setattr(original, "_worker_runtime_read_roots", lambda _: ())

    actual = original.worker_isolated_command(
        ["runtime"],
        writable_root=Path("/fixture/worktree"),
        credential_bindings=(
            (37, Path("/fixture/worktree/.credentials/auth.json")),
        ),
    )
    assert actual == json.loads(GOLDEN.read_text(encoding="utf-8"))
