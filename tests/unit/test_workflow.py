from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/checks.yml"


def test_every_bwrap_has_socket_and_ipc_containment():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    invocations = workflow.count("bwrap ")

    assert invocations == 4
    assert "--ro-bind / /" not in workflow
    for option in ("--unshare-ipc", "--unshare-uts", "--tmpfs /run", "--tmpfs /var/run"):
        assert workflow.count(option) == invocations


def test_bwrap_uses_explicit_runtime_allowlist():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for source in (
        "/usr",
        "/lib",
        "/lib64",
        "/bin",
        "/sbin",
        "/etc/alternatives",
        "/etc/ssl",
        "/etc/ld.so.cache",
        "/etc/passwd",
        "/etc/group",
        "/etc/hosts",
    ):
        assert workflow.count(f"--ro-bind {source} {source}") == 4
    assert "--ro-bind /etc/resolv.conf /etc/resolv.conf" in workflow
    assert "--ro-bind /var/lib/docker" not in workflow
    assert workflow.count('--ro-bind "$GITHUB_WORKSPACE" /tmp/workspace') == 4
    assert workflow.count('--ro-bind "$PYTHON_INSTALL_ROOT" /opt/uv-python') == 4
