"""Offline architecture links and public-interface review gate; see docs/ARCHITECTURE.md."""
import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

GUIDE = "docs/ARCHITECTURE.md"
DOCS = (GUIDE, "docs/README.md", "README.md", "AGENTS.md")
WATCHED = {
    "core/CONTRACT.md", "core/HANDOFF.md", "core/skills/README.md",
    "workflow.example.toml", "workflow.toml", "scripts/check_architecture_docs.py",
}
# Compare selected owning interfaces rather than requiring doc churn for every bug fix.
INTERFACES = {
    "src/loopzero/_cli_args.py": {"build_parser", "WAIT_TIMEOUT"},
    "src/loopzero/config.py": {"_SECTIONS", "MERGE_STRATEGIES", "REVIEWER_FAMILIES"},
    "src/loopzero/types.py": {"Config", "ResourceLimits", "ReviewConfig"},
}


def git(root, *args, text=True):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=text, timeout=60).stdout


def interface(text, names):
    nodes = []
    for node in ast.parse(text).body:
        targets = [getattr(node, "name", "")]
        if isinstance(node, ast.Assign):
            targets += [getattr(t, "id", "") for t in node.targets]
        elif isinstance(node, ast.AnnAssign):
            targets.append(getattr(node.target, "id", ""))
        if names.intersection(targets):
            nodes.append(ast.dump(node, include_attributes=False))
    return nodes


def outside_fences(text):
    fence = ""
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if marker:
            run, rest = marker.groups()
            if not fence and (run[0] == "~" or "`" not in rest):
                fence = run
                continue
            if fence and run.startswith(fence) and not rest.strip():
                fence = ""
                continue
        yield line if not fence else ""


def anchors(text):
    text = "\n".join(outside_fences(text))
    seen, result = {}, set()
    headings = re.findall(r"^ {0,3}(?:#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*|"
                          r"(\S[^\n]*?)[ \t]*\n {0,3}(?:=+|-+)[ \t]*)$", text, re.MULTILINE)
    for atx, setext in headings:
        heading = re.sub(r"(?<!\\)\[([^\]\n]*)\]\((?:[^()\n]|\([^()\n]*\))*\)", r"\1", atx or setext)
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        anchor = f"{slug}-{count}" if count else slug
        while anchor in result:
            count += 1
            anchor = f"{slug}-{count}"
        seen[slug] = count
        result.add(anchor)
    result.update(value for _, value in re.findall(
        r'''<a\b[^>]*?\s(?:id|name)\s*=\s*(["'])([^"'<>]+)\1''', text, re.IGNORECASE))
    return result


def link_errors(root):
    errors = []
    for name in DOCS:
        path = root / name
        source = "\n".join(outside_fences(path.read_text()))
        # Mask comments and inline code while keeping offsets into the source.
        text = re.sub(r"(<!--.*?-->)|\\[\\`]|(?<!`)(`+)(?!`)(?:(?!\n[ \t]*\n).)*?(?<!`)\2(?!`)",
                      lambda span: re.sub(r"[^\n]", "x", span[0]) if span.lastindex else span[0],
                      source, flags=re.DOTALL)
        links = list(re.finditer(
            r'''!?\[(?:!\[[^\]\n]*\]\((?:<[^<>\n]+>|[^<()\n]|\([^()\n]*\))*\)|[^\]]*)\]'''
            r'''\(\s*(?:<([^<>\n]+)>(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?[ \t]*|([^)]+))\)''',
            text))
        links.extend(re.finditer(
            r'''^ {0,3}\[(?!\^)[^\]\n]+\]:[ \t]*(?:<([^<>\n]+)>|([^\s<>]+))'''
            r'''(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?[ \t]*$''',
            text, re.MULTILINE))
        for link in links:
            group = 1 if link[1] else 2
            target = source[link.start(group):link.end(group)]
            if group == 2:
                target = re.split(r"""[ \t]+(?=["'])""", target.strip(), maxsplit=1)[0]
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            parent = root if parsed.path.startswith("/") else path.parent
            dest = (parent / unquote(parsed.path.removeprefix("/"))).resolve() if parsed.path else path
            if not dest.is_relative_to(root) or not dest.exists():
                errors.append(f"{name}: invalid local link {target}")
            elif (parsed.fragment and dest.is_file() and dest.suffix == ".md"
                  and unquote(parsed.fragment) not in anchors(dest.read_text())):
                errors.append(f"{name}: invalid local anchor {target}")
    return errors


def check(root, base):
    git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    tracked = git(root, "diff", "--no-renames", "--name-only", "-z", base, text=False)
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z", text=False)
    changed = set(os.fsdecode(tracked).split("\0"))
    changed.update(os.fsdecode(untracked).split("\0"))
    watched = changed.intersection(WATCHED)
    watched.update(p for p in changed if p.startswith(".github/workflows/"))
    for name, names in INTERFACES.items():
        if name not in changed:
            continue
        # Absence at the base is a real new public surface; other Git errors propagate.
        existed = git(root, "ls-tree", "--name-only", base, "--", name).strip()
        before = git(root, "show", f"{base}:{name}") if existed else ""
        after = (root / name).read_text() if (root / name).exists() else ""
        if interface(before, names) != interface(after, names):
            watched.add(name)
    errors = link_errors(root)
    if watched and GUIDE not in changed:
        messages = git(root, "log", "--format=%(trailers:key=Architecture-Review,valueonly)", f"{base}..HEAD")
        acknowledged = re.search(r"^unchanged;[ \t]*\S.{19,}$",
                                 messages, re.MULTILINE)
        if not acknowledged:
            if re.search(r"^unchanged;", messages, re.MULTILINE):
                errors.append("Architecture-Review reason must be at least 20 characters: " + ", ".join(sorted(watched)))
            else:
                errors.append("architecture sources changed without guide update or reasoned "
                              "Architecture-Review trailer: " + ", ".join(sorted(watched)))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", nargs="?")
    parser.add_argument("--links-only", action="store_true",
                        help="Validate links after merge, without the authored-history review gate")
    args = parser.parse_args()
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").removesuffix("\n")).resolve()
        if args.links_only:
            errors = link_errors(root)
        else:
            base = args.base or git(root, "merge-base", "HEAD", "origin/main").strip()
            errors = check(root, base)
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
        print(f"architecture docs: FAIL ({exc})", file=sys.stderr)
        return 1
    if errors:
        print("\n".join(errors), file=sys.stderr)
    print("architecture docs: " + ("FAIL" if errors else "PASS"))
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
