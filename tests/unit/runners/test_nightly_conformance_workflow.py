"""Static checks for the live nightly workflow's containment plumbing."""

from conftest import REPO


def test_workflow_binds_every_venv_interpreter_symlink_target() -> None:
    workflow = (REPO / ".github/workflows/nightly-conformance.yml").read_text(
        encoding="utf-8"
    )
    validation_step = workflow.index(
        "- name: Run live scenarios in the validation bubble"
    )
    validation_body = workflow[validation_step:]

    assert 'PYTHON_BIN=$(readlink -f "$(uv python find 3.13)")' in validation_body
    assert 'for interpreter in "$ROOT"/.venv/bin/python*; do' in validation_body
    assert 'target=$(readlink -f "$interpreter")' in validation_body
    assert '"$PYTHON_INSTALL_ROOT"/*) ;;' in validation_body
    bind = '--ro-bind "$PYTHON_INSTALL_ROOT" "$PYTHON_INSTALL_ROOT"'
    assert bind in validation_body
    assert validation_body.index('--tmpfs "$ACCOUNT_HOME"') < validation_body.index(
        bind
    )
