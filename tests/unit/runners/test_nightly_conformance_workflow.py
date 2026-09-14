"""Static checks for the live nightly workflow's containment plumbing."""

from conftest import REPO


def test_workflow_recreates_venv_interpreter_aliases_before_binding_target() -> None:
    workflow = (REPO / ".github/workflows/nightly-conformance.yml").read_text(
        encoding="utf-8"
    )
    validation_step = workflow.index(
        "- name: Run live scenarios in the validation bubble"
    )
    validation_body = workflow[validation_step:]

    assert 'PYTHON_BIN=$(readlink -f "$(uv python find 3.13)")' in validation_body
    assert 'for interpreter in "$ROOT"/.venv/bin/python*; do' in validation_body
    assert 'raw_target=$(readlink "$interpreter")' in validation_body
    assert 'resolved_target=$(readlink -f "$raw_target")' in validation_body
    assert '"$PYTHON_INSTALL_ROOT"/*) ;;' in validation_body
    alias = 'PYTHON_ALIAS_LAYOUT+=(--symlink "$(readlink "$dir")" "$dir")'
    assert alias in validation_body
    bind = '--ro-bind "$PYTHON_INSTALL_ROOT" "$PYTHON_INSTALL_ROOT"'
    assert bind in validation_body
    assert validation_body.index('--tmpfs "$ACCOUNT_HOME"') < validation_body.index(
        '"${PYTHON_ALIAS_LAYOUT[@]}"'
    )
    assert validation_body.index('"${PYTHON_ALIAS_LAYOUT[@]}"') < validation_body.index(
        bind
    )
