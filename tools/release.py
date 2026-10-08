#!/usr/bin/env python3
"""Cut a framework release: version, template pin, release notes, commit, tag, push, GitHub release.

    python tools/release.py 0.2.0             # dry run: check, then print every step
    python tools/release.py --bump minor      # the same, next minor version
    python tools/release.py --bump minor --apply   # commit and tag locally; push nothing
    python tools/release.py --bump minor --push    # also push main + tag, create the release
    python tools/release.py --bootstrap-pin --push # a fresh repository, once, before its first release

Maintainers only; see docs/internals/RELEASING.md. Refuses unless the checkout is `main`, has
no uncommitted or untracked files and equals `origin/main` after a fetch. The hermetic suites run
before any file changes (`--skip-tests` skips them, with a warning). The notes are either written
by hand (`--notes-file`, required for the first release) or the titles of the pull requests merged
since the previous `vX.Y.Z` tag; generated notes need a deny-pattern file (`--deny-patterns` or
RELEASE_DENY_PATTERNS) and are refused if any title matches. The notes travel in the annotated tag.

A release refuses while the consumer template's pin names nothing in this repository, which is the
state of a fresh repository made from another one's tree. `--bootstrap-pin`, before the first
release only, pins the template to the current commit and commits that.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECTS = ("pyproject.toml", "scripts/live/pyproject.toml", "scripts/integration/pyproject.toml")
NOTES = "RELEASE-NOTES.md"
NOTES_HEADER = "# Release notes\n\nOne entry per release, newest first: the pull requests it contains.\n"
# The hermetic suites: (directory, arguments after the interpreter). No Docker, no Striim.
SUITES = [
    (".", ["-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]),
    ("scripts/live", ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "not live", "tests"]),
    ("scripts/integration", ["-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "not docker", "tests"]),
]
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SQUASH = re.compile(r"^(?P<title>.+) \(#(?P<n>\d+)\)$")
_MERGE = re.compile(r"^Merge pull request #(?P<n>\d+) from \S+")
# Every literal a package reports as its version at run time; tests/test_release.py checks the list.
RUNTIME_VERSIONS = ("scripts/cli/striim_test/__init__.py", "scripts/live/livetest/__init__.py",
                    "scripts/integration/inttest/__init__.py")
# The consumer template's pin: a release names itself, so a repo made from the template syncs to it.
TEMPLATE_PIN = "templates/consumer-repo/framework.pin"
_RUNTIME_LINE = re.compile(r'^__version__\s*=\s*"([^"]*)"', re.M)
# Always checked. Names (customers, hosts, private repositories) come from the caller's file, one
# `<kind> <regex>` per line, `#` comments allowed.
BUILTIN_DENY = [
    ("path", r"/home/[a-z][a-z0-9_-]+/"), ("path", r"/Users/[A-Za-z][A-Za-z0-9_.-]+/"),
    ("host", r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), ("host", r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    ("host", r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
]


class Refused(Exception):
    pass


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode:
        raise Refused(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else None


def parse(version):
    m = SEMVER.fullmatch(version)
    if not m:
        raise Refused(f"version {version!r} is not X.Y.Z")
    return tuple(int(p) for p in m.groups())


def bump(version, part):
    major, minor, patch = parse(version)
    return {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0",
            "patch": f"{major}.{minor}.{patch + 1}"}[part]


def _project_table(text):
    """(start, end) of the [project] table's body."""
    m = re.search(r"^\[project\]\s*$", text, re.M)
    if not m:
        raise Refused("no [project] table")
    nxt = re.search(r"^\[", text[m.end():], re.M)
    return m.end(), m.end() + nxt.start() if nxt else len(text)


_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"]*)"', re.M)


def read_version(text):
    start, end = _project_table(text)
    m = _VERSION_LINE.search(text, start, end)
    if not m:
        raise Refused("no version in [project]")
    return m.group(1)


def set_version(text, version):
    start, end = _project_table(text)
    m = _VERSION_LINE.search(text, start, end)
    if not m:
        raise Refused("no version in [project]")
    return text[:m.start()] + f'version = "{version}"' + text[m.end():]


def read_runtime_version(text):
    m = _RUNTIME_LINE.search(text)
    if not m:
        raise Refused("no __version__ literal")
    return m.group(1)


def set_runtime_version(text, version):
    read_runtime_version(text)
    return _RUNTIME_LINE.sub(f'__version__ = "{version}"', text, count=1)


def load_patterns(path):
    patterns = list(BUILTIN_DENY)
    if path:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise Refused(f"deny-pattern file: {e}")
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                kind, _, pat = line.partition(" ")
                patterns.append((kind, pat.strip()))
    try:
        return [(kind, re.compile(pat)) for kind, pat in patterns]
    except re.error as e:
        raise Refused(f"deny-pattern file: bad pattern: {e}")


def deny_hits(labelled, patterns):
    """`label: text  [kind: match]` for each (label, text) a pattern matches."""
    return [f"{label}: {text}  [{kind}: {m.group(0)}]" for label, text in labelled
            for kind, rx in patterns if (m := rx.search(text))]


def previous_tag(repo):
    """The highest vX.Y.Z tag reachable from HEAD, or None."""
    tags = (git(repo, "tag", "--merged", "HEAD", "--list", "v*") or "").split()
    tags = [t for t in tags if SEMVER.fullmatch(t[1:])]
    return max(tags, key=lambda t: parse(t[1:])) if tags else None


def pull_requests(repo, since):
    """(number, title) of each pull request merged on main after `since` (all history if None)."""
    span = f"{since}..HEAD" if since else "HEAD"
    log = git(repo, "log", "--first-parent", "--reverse", "--format=%s%x1f%b%x1e", span) or ""
    found = {}
    for record in log.split("\x1e"):
        if not record.strip():
            continue
        subject, _, body = record.strip("\n").partition("\x1f")
        if m := _SQUASH.match(subject):
            found[int(m["n"])] = m["title"].strip()
        elif m := _MERGE.match(subject):
            title = next((line.strip() for line in body.splitlines() if line.strip()), subject)
            found[int(m["n"])] = title
    return sorted(found.items())


def notes_body(prs):
    lines = [f"- {title} (#{n})" for n, title in prs] or ["- No pull requests merged since the previous release."]
    return "\n".join(lines) + "\n"


def notes_entry(version, prs, date, body=None):
    return f"## v{version} ({date})\n\n" + (body.strip() + "\n" if body is not None else notes_body(prs))


def add_entry(existing, entry):
    """The notes file with `entry` as its newest entry."""
    text = existing if existing is not None else NOTES_HEADER
    m = re.search(r"^## ", text, re.M)
    if m:
        return text[:m.start()] + entry + "\n" + text[m.start():]
    return text.rstrip("\n") + "\n\n" + entry


def preflight(repo, remote, fetch, notes_file=None):
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch != "main":
        raise Refused(f"not on main (on {branch or 'a detached HEAD'})")
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise Refused("uncommitted tracked changes; commit or stash them first")
    allowed = set()
    if notes_file and notes_file.resolve().is_relative_to(repo):
        allowed.add(notes_file.resolve().relative_to(repo).as_posix())
    untracked = [p for p in (git(repo, "ls-files", "--others", "--exclude-standard") or "").splitlines()
                 if p not in allowed]
    if untracked:
        raise Refused("untracked files (move them out or ignore them; only --notes-file may be untracked): "
                      + ", ".join(untracked[:10]))
    if fetch:
        git(repo, "fetch", "--quiet", "--tags", remote, "main")
    upstream = git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/main", check=False)
    if upstream is None:
        raise Refused(f"no {remote}/main to compare with")
    if upstream != git(repo, "rev-parse", "HEAD"):
        raise Refused(f"main is not equal to {remote}/main; pull or push first")


def consumer_pins(repo):
    """The starter and worked consumer repos share the release pin lifecycle."""
    return [TEMPLATE_PIN, *sorted(str(p.relative_to(repo))
                                 for p in repo.glob("examples/*/framework.pin"))]


def pin_resolves(repo, relative=TEMPLATE_PIN):
    """The template pin (a full commit or a tag) names a commit of this repository."""
    path = repo / relative
    words = path.read_text().split() if path.exists() else []
    if not words:
        return False
    ref = words[0] if re.fullmatch(r"[0-9a-fA-F]{40}", words[0]) else f"refs/tags/{words[0]}"
    return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False) is not None


def bootstrap_pin(repo, args, dry):
    """Pin the template to HEAD so the first release's suites pass; the release then moves it to its tag."""
    prev = previous_tag(repo)
    if prev:
        raise Refused(f"--bootstrap-pin is for a repository's first release; {prev} exists, "
                      f"and each release moves the pin to its own tag")
    invalid = [relative for relative in consumer_pins(repo) if not pin_resolves(repo, relative)]
    if not invalid:
        print(f"{TEMPLATE_PIN} and worked consumer pins already name commits of this repository; nothing to do.")
        return 0
    head = git(repo, "rev-parse", "HEAD")
    if dry:
        print("DRY RUN: nothing is changed. --apply commits locally; --push also pushes main.")
    for relative in invalid:
        pin = repo / relative
        old = pin.read_text().strip() if pin.exists() else "(none)"
        step(f"{relative}: pin {old} -> {head}")
        if not dry:
            pin.write_text(head + "\n")
    commands = [["git", "add", *invalid],
                ["git", "commit", "-q", "-m", "Pin consumer repositories to this repository's baseline"],
                ["git", "push", args.remote, "main"]]
    for cmd in commands:
        step(shlex.join(cmd))
        if dry or (cmd[1] == "push" and not args.push):
            continue
        git(repo, *cmd[1:])
    print("DRY RUN complete." if dry else "Pin pushed; run the release next." if args.push
          else "Pin committed, not pushed: push main, then run the release.")
    return 0


def plan(repo, args):
    current = {rel: read_version((repo / rel).read_text()) for rel in PYPROJECTS}
    current.update({rel: read_runtime_version((repo / rel).read_text()) for rel in RUNTIME_VERSIONS})
    invalid = [relative for relative in consumer_pins(repo) if not pin_resolves(repo, relative)]
    if invalid:
        raise Refused(f"{invalid[0]} names nothing in this repository; a fresh repository runs "
                      f"`tools/release.py --bootstrap-pin --push` once before its first release")
    base = current[PYPROJECTS[0]]
    version = bump(base, args.bump) if args.bump else args.version
    parse(version)
    tag = f"v{version}"
    if git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", check=False):
        raise Refused(f"tag {tag} already exists")
    prev = previous_tag(repo)
    if prev and parse(version) <= parse(prev[1:]):
        raise Refused(f"{version} is not above the previous release {prev}")
    date = datetime.date.today().isoformat()
    if args.notes_file:
        try:
            body = args.notes_file.read_text(encoding="utf-8")
        except OSError as e:
            raise Refused(f"--notes-file: {e}")
        if not body.strip():
            raise Refused(f"--notes-file {args.notes_file} is empty")
        labelled = [(f"{args.notes_file.name} line {i}", line) for i, line in enumerate(body.splitlines(), 1)]
        hits = deny_hits(labelled, load_patterns(args.deny_patterns))
        entry = notes_entry(version, None, date, body)
    else:
        if not prev:
            # The history before the first release came from another repository, PR numbers and all.
            raise Refused("no previous vX.Y.Z tag: write the first release's notes by hand and pass --notes-file")
        if not args.deny_patterns:
            raise Refused("generated notes need a deny-pattern file (--deny-patterns or RELEASE_DENY_PATTERNS); "
                          "or write the notes by hand and pass --notes-file")
        prs = pull_requests(repo, prev)
        hits = deny_hits([(f"#{n}", title) for n, title in prs], load_patterns(args.deny_patterns))
        entry = notes_entry(version, prs, date)
    if hits:
        raise Refused("the release notes match the deny patterns; edit them into a --notes-file:\n  "
                      + "\n  ".join(hits))
    return version, tag, prev, current, entry


def step(text):
    print(f"==> {text}", flush=True)


def run_suites(repo, python, dry):
    for where, argv in SUITES:
        cwd = repo / where
        step(f"(cd {where} && {shlex.join([python, *argv])})")
        if dry:
            continue
        if subprocess.run([python, *argv], cwd=cwd).returncode:
            raise SystemExit(f"suite failed in {where}; nothing was changed")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("version", nargs="?", help="the release version, X.Y.Z")
    p.add_argument("--bump", choices=("patch", "minor", "major"), help="next version from pyproject.toml")
    p.add_argument("--apply", action="store_true", help="change files, commit and tag locally; push nothing")
    p.add_argument("--push", action="store_true", help="--apply, then push main + the tag and create the GitHub release")
    p.add_argument("--bootstrap-pin", action="store_true",
                   help="before the first release: pin the consumer template to the current commit")
    p.add_argument("--skip-tests", action="store_true", help="do not run the hermetic suites")
    p.add_argument("--python", default=sys.executable, help="interpreter for the suites (default: this one)")
    p.add_argument("--notes-file", type=Path, help="hand-written notes (Markdown) instead of generated ones")
    p.add_argument("--deny-patterns", default=os.environ.get("RELEASE_DENY_PATTERNS") or None,
                   help="`<kind> <regex>` lines the notes must not match (default: $RELEASE_DENY_PATTERNS)")
    p.add_argument("--remote", default="origin")
    p.add_argument("--no-fetch", action="store_true", help="compare with the last fetched origin/main")
    p.add_argument("--repo", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.bootstrap_pin:
        if args.version or args.bump:
            p.error("--bootstrap-pin takes no version")
    elif bool(args.version) == bool(args.bump):
        p.error("give a version or --bump, not both")
    repo, dry = args.repo.resolve(), not (args.apply or args.push)
    gh = os.environ.get("RELEASE_GH") or "gh"
    try:
        preflight(repo, args.remote, fetch=not args.no_fetch, notes_file=args.notes_file)
        if args.bootstrap_pin:
            return bootstrap_pin(repo, args, dry)
        version, tag, prev, current, entry = plan(repo, args)
    except Refused as e:
        print(f"release: refused: {e}", file=sys.stderr)
        return 2
    if dry:
        print("DRY RUN: nothing is changed. --apply commits and tags locally; --push also publishes.")
    print(f"release {tag} (previous: {prev or 'none'}; main = {git(repo, 'rev-parse', '--short', 'HEAD')})")

    if args.skip_tests:
        print("WARNING: --skip-tests: the hermetic suites were NOT run for this release.", file=sys.stderr)
    else:
        try:
            run_suites(repo, args.python, dry)
        except SystemExit as e:
            print(f"release: {e}", file=sys.stderr)
            return 1

    pins = consumer_pins(repo)
    for relative in pins:
        pin = repo / relative
        step(f"{relative}: pin {pin.read_text().strip() if pin.exists() else '(none)'} -> {tag}")
        if not dry:
            pin.write_text(tag + "\n")
    for rel, old in current.items():
        step(f"{rel}: version {old} -> {version}")
        if not dry:
            path = repo / rel
            setter = set_version if rel in PYPROJECTS else set_runtime_version
            path.write_text(setter(path.read_text(), version))
    notes = repo / NOTES
    step(f"{NOTES}: add this entry\n{entry}")
    if not dry:
        notes.write_text(add_entry(notes.read_text() if notes.exists() else None, entry))

    body = entry.split("\n", 2)[2]                       # the entry without its heading
    commands = [(["git", "add", *current, *pins, NOTES], None),
                (["git", "commit", "-q", "-m", f"Release {tag}"], None),
                (["git", "tag", "-a", tag, "--cleanup=verbatim", "-m", body], "<the entry above, without its heading>")]
    for cmd, shown in commands:
        step(shlex.join(cmd[:-1]) + " " + shown if shown else shlex.join(cmd))
        if not dry:
            git(repo, *cmd[1:])

    # The notes are the tag's message, so these two work as printed, now or later.
    push = ["git", "push", "--atomic", args.remote, "main", tag]
    gh_cmd = [gh, "release", "create", tag, "--verify-tag", "--title", tag, "--notes-from-tag"]
    for cmd in (push, gh_cmd):
        step(shlex.join(cmd))
        if args.push and subprocess.run(cmd, cwd=repo).returncode:
            print(f"release: {cmd[0]} failed; the commit and tag {tag} are local. Fix it, then run the "
                  "remaining printed steps from the checkout", file=sys.stderr)
            return 1
    if not args.push:
        print(f"Not pushed. Review with `git show {tag}`; publish with the two steps above, from this checkout."
              if args.apply else "DRY RUN complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
