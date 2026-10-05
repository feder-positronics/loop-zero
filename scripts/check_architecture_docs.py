"""Offline architecture links and public-interface review gate; see docs/ARCHITECTURE.md."""
import argparse
import ast
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
    "src/loopzero/cli.py": {"build_parser"},
    "src/loopzero/config.py": {"_SECTIONS", "MERGE_STRATEGIES", "REVIEWER_FAMILIES"},
    "src/loopzero/types.py": {"Config", "ResourceLimits", "ReviewConfig"},
}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


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


def anchors(text):
    seen, result = {}, set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)(?:\s+#+)?$", text, re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    result.update(re.findall(r'<a\s+(?:name|id)=["\']([^"\']+)', text))
    return result


def link_errors(root):
    errors = []
    for name in DOCS:
        path = root / name
        text = path.read_text()
        # Ignore fenced examples, then handle inline links and reference definitions.
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        links = re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text)
        links += re.findall(r"^\s*\[[^\]]+\]:\s*(\S+)", text, re.MULTILINE)
        for raw in links:
            target = raw.strip().split(' "', 1)[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            dest = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
            if not dest.is_relative_to(root) or not dest.exists():
                errors.append(f"{name}: invalid local link {target}")
            elif (parsed.fragment and dest.is_file() and dest.suffix == ".md"
                  and unquote(parsed.fragment) not in anchors(dest.read_text())):
                errors.append(f"{name}: invalid local anchor {target}")
    return errors


def check(root, base):
    git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    changed = set(git(root, "diff", "--name-only", "-z", base).split("\0"))
    changed.update(git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
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
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip()).resolve()
        if args.links_only:
            errors = link_errors(root)
        else:
            base = args.base or git(root, "merge-base", "HEAD", "origin/main").strip()
            errors = check(root, base)
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as exc:
        print(f"architecture docs: FAIL ({exc})", file=sys.stderr)
        return 1
    if errors:
        print("\n".join(errors), file=sys.stderr)
    print("architecture docs: " + ("FAIL" if errors else "PASS"))
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
