import os
import re
import subprocess
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "SETUP.md"


def setup_commands(shell, prefix, *, text=None):
    return next(block for block in re.findall(rf"```{shell}\n(.*?)```", (SETUP.read_text() if text is None else text), re.S)
                if block.startswith(prefix))


def test_agent_skill_links_install_and_upgrade_without_gnu_mv(tmp_path, fake_tool):
    home = tmp_path / "home"
    home.mkdir()
    fake_tool("mv", '[ "$1" != "-T" ] || exit 64\nexec /bin/mv "$@"\n')
    env = dict(os.environ, HOME=str(home))
    commands = setup_commands("sh", "CHECKOUT=").replace("/path/to/loop-zero-checkout", '"$CHECKOUT"')
    source = home / ".local/share/loopzero/source"
    first_checkout = tmp_path / "first checkout"
    for checkout in (first_checkout, tmp_path / "new checkout"):
        skill = checkout / "core/skills/example"
        skill.mkdir(parents=True)
        subprocess.run(["sh", "-ec", commands], env=dict(env, CHECKOUT=str(checkout)), check=True)
        assert source.readlink() == checkout
        for agent in (".agents", ".claude"):
            assert (home / agent / "skills/example").resolve(strict=True) == skill
        assert sorted(entry.name for entry in first_checkout.iterdir()) == ["core"]


def test_api_key_permissions_without_gnu_stat(tmp_path, fake_tool):
    fake_tool("stat", '[ "$1" != "-c" ] || exit 64\nexec /usr/bin/stat "$@"\n')
    directory = tmp_path / ".config/typesafe"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    key = directory / "api-key"
    key.write_text("placeholder")
    key.chmod(0o600)
    commands = setup_commands("bash", 'test -r "$HOME/.config/typesafe/api-key"')
    env = dict(os.environ, HOME=str(tmp_path))
    for contents, code in (("placeholder", 0), ("", 1)):
        key.write_text(contents)
        result = subprocess.run(["bash", "-c", commands], env=env, capture_output=True, text=True, check=False)
        assert result.returncode == code
        assert result.stdout.splitlines() == ([f"700 {directory}", f"600 {key}"] if contents else [])
