"""Trusted Python runtime behavior at the validation sandbox boundary."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loopzero.kernel import sandbox

from .capabilities import NAMESPACE_AVAILABLE, NAMESPACE_REASON


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["/usr/bin/git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _worktree(root: Path) -> Path:
    worktree = root / "worktree"
    worktree.mkdir()
    _git(worktree, "init", "-q", "-b", "main")
    (worktree / "tracked").write_text("base\n", encoding="utf-8")
    _git(worktree, "add", ".")
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "base",
    )
    return worktree


def _compiled_runtime(tmp_path: Path, *, runpath: str) -> tuple[Path, str]:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler unavailable")
    runtime = tmp_path / "runtime"
    interpreter = runtime / "bin" / "python"
    library_dir = runtime / "lib"
    interpreter.parent.mkdir(parents=True)
    library_dir.mkdir()
    hidden_library_dir = tmp_path / "hidden-prefix" / "lib"
    hidden_library_dir.mkdir(parents=True)
    library_name = "libpython-loopzero-fixture.so.1.0"
    source = tmp_path / "library.c"
    source.write_text("int python_fixture(void) { return 14; }\n", encoding="utf-8")
    hidden_library = hidden_library_dir / library_name
    subprocess.run(
        [
            compiler,
            "-shared",
            "-fPIC",
            f"-Wl,-soname,{library_name}",
            "-o",
            str(hidden_library),
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    shutil.copy2(hidden_library, library_dir / library_name)
    launcher = tmp_path / "launcher.c"
    launcher.write_text(
        "#include <stdio.h>\n"
        "int python_fixture(void);\n"
        'int main(void) { printf("%d\\n", python_fixture()); return 0; }\n',
        encoding="utf-8",
    )
    subprocess.run(
        [
            compiler,
            str(launcher),
            "-Wl,--no-as-needed",
            f"-L{hidden_library_dir}",
            f"-l:{library_name}",
            f"-Wl,-rpath,{runpath}",
            "-o",
            str(interpreter),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return runtime, library_name


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_trusted_python_runtime_repairs_hidden_absolute_runpath(tmp_path: Path) -> None:
    worktree = _worktree(tmp_path)
    hidden = tmp_path / "hidden-prefix" / "lib"
    runtime, library_name = _compiled_runtime(tmp_path, runpath=str(hidden))
    interpreter = runtime / "bin" / "python"

    host = subprocess.run(
        [str(interpreter)], check=True, capture_output=True, text=True, timeout=10
    )
    assert host.stdout.strip() == "14"

    before = sandbox.run_validation_child(
        [str(interpreter)],
        worktree=worktree,
        read_only_roots=(runtime,),
        timeout=10,
    )
    assert before.returncode != 0
    assert library_name in before.stderr

    trusted = sandbox.trusted_python_runtime(
        interpreter=interpreter,
        base=runtime,
        forbidden_roots=(worktree,),
    )
    after = sandbox.run_validation_child(
        [str(interpreter)],
        worktree=worktree,
        python_runtime=trusted,
        source_environment={
            **os.environ,
            "LD_LIBRARY_PATH": str(tmp_path / "hostile-library-path"),
            "LD_PRELOAD": str(tmp_path / "hostile-preload.so"),
        },
        timeout=10,
    )
    assert after.returncode == 0, after.stderr
    assert after.stdout.strip() == "14"

    raw = subprocess.run(
        sandbox.validation_command(
            [str(interpreter)],
            worktree=worktree,
            python_runtime=trusted,
        ),
        env={
            **os.environ,
            "LD_LIBRARY_PATH": str(tmp_path / "hostile-library-path"),
            "LD_PRELOAD": str(tmp_path / "hostile-preload.so"),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert raw.returncode == 0, raw.stderr
    assert raw.stdout.strip() == "14"

    inspected = sandbox.run_validation_child(
        ["/usr/bin/env"],
        worktree=worktree,
        python_runtime=trusted,
        source_environment={
            **os.environ,
            "LD_LIBRARY_PATH": str(tmp_path / "hostile-library-path"),
            "LD_PRELOAD": str(tmp_path / "hostile-preload.so"),
        },
        timeout=10,
    )
    assert inspected.returncode == 0, inspected.stderr
    assert f"LD_LIBRARY_PATH={runtime / 'lib'}" in inspected.stdout
    assert "hostile-library-path" not in inspected.stdout
    assert "LD_PRELOAD=" not in inspected.stdout


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_relative_runpath_and_generic_children_need_no_runtime_contract(
    tmp_path: Path,
) -> None:
    worktree = _worktree(tmp_path)
    runtime, _library_name = _compiled_runtime(tmp_path, runpath="$ORIGIN/../lib")
    relative = sandbox.run_validation_child(
        [str(runtime / "bin" / "python")],
        worktree=worktree,
        read_only_roots=(runtime,),
        timeout=10,
    )
    assert relative.returncode == 0, relative.stderr
    assert relative.stdout.strip() == "14"

    generic = sandbox.run_validation_child(
        ["/usr/bin/env"],
        worktree=worktree,
        source_environment={
            **os.environ,
            "LD_LIBRARY_PATH": "/hostile/library",
            "LD_PRELOAD": "/hostile/preload.so",
        },
        timeout=10,
    )
    assert generic.returncode == 0, generic.stderr
    assert "LD_LIBRARY_PATH=" not in generic.stdout
    assert "LD_PRELOAD=" not in generic.stdout


@pytest.mark.skipif(not NAMESPACE_AVAILABLE, reason=NAMESPACE_REASON)
def test_runtime_without_a_private_library_directory_remains_supported(
    tmp_path: Path,
) -> None:
    worktree = _worktree(tmp_path)
    runtime = tmp_path / "runtime"
    interpreter = runtime / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nprintf 'libraryless\\n'\n", encoding="utf-8")
    interpreter.chmod(0o755)
    trusted = sandbox.trusted_python_runtime(
        interpreter=interpreter,
        base=runtime,
        forbidden_roots=(worktree,),
    )
    assert trusted.library_path is None

    completed = sandbox.run_validation_child(
        [str(interpreter)],
        worktree=worktree,
        python_runtime=trusted,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "libraryless"


def test_trusted_python_runtime_rejects_untrusted_layouts(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runtime = tmp_path / "runtime"
    interpreter = runtime / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)

    with pytest.raises(TypeError, match="trusted_python_runtime"):
        sandbox.TrustedPythonRuntime()
    with pytest.raises(sandbox.SandboxError, match="overlaps a writable source"):
        sandbox.trusted_python_runtime(
            interpreter=interpreter,
            base=tmp_path,
            forbidden_roots=(worktree,),
        )

    outside = tmp_path / "outside-python"
    outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outside.chmod(0o755)
    interpreter.unlink()
    interpreter.symlink_to(outside)
    with pytest.raises(sandbox.SandboxError, match="escapes its runtime base"):
        sandbox.trusted_python_runtime(
            interpreter=interpreter,
            base=runtime,
            forbidden_roots=(worktree,),
        )

    for syntax in ("runtime:hostile", "runtime;hostile", "runtime$ORIGIN"):
        separated = tmp_path / syntax
        separated_interpreter = separated / "bin" / "python"
        separated_interpreter.parent.mkdir(parents=True)
        separated_interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        separated_interpreter.chmod(0o755)
        with pytest.raises(sandbox.SandboxError, match="loader syntax"):
            sandbox.trusted_python_runtime(
                interpreter=separated_interpreter,
                base=separated,
                forbidden_roots=(worktree,),
            )


def test_trusted_python_runtime_rejects_library_symlink_escape(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runtime = tmp_path / "runtime"
    interpreter = runtime / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    external_library = tmp_path / "external-library"
    external_library.mkdir()
    (runtime / "lib").symlink_to(external_library, target_is_directory=True)

    with pytest.raises(sandbox.SandboxError, match="direct symlink|escapes its base"):
        sandbox.trusted_python_runtime(
            interpreter=interpreter,
            base=runtime,
            forbidden_roots=(worktree,),
        )


@pytest.mark.parametrize("mutation", ["symlink", "world-writable"])
def test_runtime_is_revalidated_after_selection(
    tmp_path: Path, mutation: str
) -> None:
    worktree = _worktree(tmp_path)
    runtime = tmp_path / "runtime"
    interpreter = runtime / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    library = runtime / "lib"
    library.mkdir()
    trusted = sandbox.trusted_python_runtime(
        interpreter=interpreter,
        base=runtime,
        forbidden_roots=(worktree,),
    )

    if mutation == "symlink":
        library.rmdir()
        external = tmp_path / "external-library"
        external.mkdir()
        library.symlink_to(external, target_is_directory=True)
    else:
        library.chmod(0o777)

    with pytest.raises(sandbox.SandboxError, match="direct symlink|not protected"):
        sandbox.validation_command(
            [str(interpreter)],
            worktree=worktree,
            python_runtime=trusted,
        )
