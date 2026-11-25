import os
import re
import subprocess
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "SETUP.md"


def test_api_key_permissions_without_gnu_stat(tmp_path, fake_tool):
    fake_tool("stat", '[ "$1" != "-c" ] || exit 64\nexec /usr/bin/stat "$@"\n')
    directory = tmp_path / ".config/typesafe"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    key = directory / "api-key"
    key.write_text("placeholder")
    key.chmod(0o600)
    commands = next(
        block for block in re.findall(r"```bash\n(.*?)```", SETUP.read_text(), re.S)
        if block.startswith('test -r "$HOME/.config/typesafe/api-key"')
    )
    env = dict(os.environ, HOME=str(tmp_path))
    result = subprocess.run(["bash", "-c", commands], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == [f"700 {directory}", f"600 {key}"]

    key.write_text("")
    result = subprocess.run(["bash", "-c", commands], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout == ""
