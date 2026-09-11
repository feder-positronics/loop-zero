import stat
from types import SimpleNamespace

import pytest

from loopzero import trust as trusted_executable


@pytest.mark.parametrize(
    ("owner", "mode", "expected"),
    [
        (0, stat.S_IFREG | 0o755, False),
        (0, stat.S_IFREG | 0o775, True),
        (1000, stat.S_IFREG | 0o755, True),
    ],
)
def test_root_trusts_only_root_owned_non_group_writable_system_paths(
    monkeypatch, owner, mode, expected
) -> None:
    monkeypatch.setattr(trusted_executable.os, "geteuid", lambda: 0)

    assert (
        trusted_executable._writable_by_current_account(
            SimpleNamespace(st_uid=owner, st_gid=0, st_mode=mode)
        )
        is expected
    )


def test_non_root_owner_writable_system_path_remains_untrusted(monkeypatch) -> None:
    monkeypatch.setattr(trusted_executable.os, "geteuid", lambda: 1000)

    assert trusted_executable._writable_by_current_account(
        SimpleNamespace(st_uid=1000, st_gid=1000, st_mode=stat.S_IFREG | 0o755)
    )


def test_trusted_subprocess_environment_removes_loader_injection() -> None:
    assert trusted_executable.trusted_subprocess_environment(
        {
            "PATH": "/usr/bin",
            "GH_TOKEN": "retained-authority",
            "LD_PRELOAD": "/attacker/preload.so",
            "LD_LIBRARY_PATH": "/attacker/lib",
            "DYLD_INSERT_LIBRARIES": "/attacker/inject.dylib",
        }
    ) == {"PATH": "/usr/bin", "GH_TOKEN": "retained-authority"}
