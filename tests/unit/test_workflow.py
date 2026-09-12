from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/checks.yml"
# The validation sandbox binds these as the runner has them (merged-/usr
# symlinks or read-only directories) so the kernel's mount model sees the
# real root layout; the other sandboxes bind them as directories.
HOST_LAYOUT_ENTRIES = ("/bin", "/lib", "/lib64", "/sbin")


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
        expected = 3 if source in HOST_LAYOUT_ENTRIES else 4
        assert workflow.count(f"--ro-bind {source} {source}") == expected
    assert workflow.count('"${ROOT_LAYOUT[@]}"') == 1
    assert "for entry in /bin /lib /lib64 /sbin; do" in workflow
    assert 'ROOT_LAYOUT+=(--symlink "$(readlink "$entry")" "$entry")' in workflow
    assert 'ROOT_LAYOUT+=(--ro-bind "$entry" "$entry")' in workflow
    assert workflow.count("ROOT_LAYOUT+=(") == 2
    assert "--ro-bind /etc/resolv.conf /etc/resolv.conf" in workflow
    assert "--ro-bind /var/lib/docker" not in workflow
    assert workflow.count('--ro-bind "$GITHUB_WORKSPACE" /tmp/workspace') == 4
    assert workflow.count('--ro-bind "$PYTHON_INSTALL_ROOT" /opt/uv-python') == 4
