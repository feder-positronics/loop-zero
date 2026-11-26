import os
import re
import subprocess
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "SETUP.md"


def test_agent_skill_links_install_and_upgrade_without_gnu_mv(tmp_path, fake_tool):
    home = tmp_path / "home"
    home.mkdir()
    fake_tool("mv", '[ "$1" != "-T" ] || exit 64\nexec /bin/mv "$@"\n')
    env = dict(os.environ, HOME=str(home))
    setup_block = next(
        block for block in re.findall(r"```sh\n(.*?)```", SETUP.read_text(), re.S)
        if block.startswith("CHECKOUT=")
    )
    source = home / ".local/share/loopzero/source"
    first_checkout = tmp_path / "first checkout"
    for checkout in (first_checkout, tmp_path / "new checkout"):
        skill = checkout / "core/skills/example"
        skill.mkdir(parents=True)
        commands = setup_block.replace("/path/to/loop-zero-checkout", '"$CHECKOUT"')
        subprocess.run(["sh", "-ec", commands], env=dict(env, CHECKOUT=str(checkout)), check=True)
        assert source.readlink() == checkout
        for agent in (".agents", ".claude"):
            assert (home / agent / "skills/example").resolve(strict=True) == skill
        assert sorted(entry.name for entry in first_checkout.iterdir()) == ["core"]

    reference = SETUP.parent / "core/skills/work-issue/reference.md"
    reentry = reference.read_text().split("## Re-entry\n", 1)[1].split("\n## ", 1)[0]
    assert "(../../CONTRACT.md#the-six-steps)" in reentry
