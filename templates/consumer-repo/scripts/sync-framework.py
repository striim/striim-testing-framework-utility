#!/usr/bin/env python3
"""Put the framework checkout at exactly the version in framework.pin, and install it.

The pin is a release tag (preferred) or a full 40-character commit. The checkout is read-only:
it is left at a detached HEAD, and a checkout with local changes is refused, never overwritten.

Run it with any python3. Inside a venv it installs into that venv. Otherwise it installs into this
repo's .venv, making it first with a Python the pinned framework supports (the requires-python in
its pyproject.toml); a venv with an older Python is refused. When .env is missing, it copies
.env.example to it.

    python3 scripts/sync-framework.py              # clone or update, then install
    python3 scripts/sync-framework.py --check      # exit 1 unless the checkout is clean, detached, at the pin
    python3 scripts/sync-framework.py --dry-run    # say what would happen
    python3 scripts/sync-framework.py --find-links DIR   # install from a local wheelhouse, no index
    python3 scripts/sync-framework.py --python PATH      # make .venv with this Python

Exit codes: 0 done, 1 --check found a difference (or local changes), 2 a bad pin or setting, or no
suitable Python, 3 refused (the folder is not a framework checkout, or it has local changes), 4 git
or network failure, 5 install failure.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REMOTE = "https://github.com/striim/striim-testing-framework-utility.git"
DEFAULT_HOME = REPO.parent / "striim-testing-framework-utility"
SHA = re.compile(r"[0-9a-f]{40}")


class SyncError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def git(home: Path, *args: str, check: bool = True) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(home), *args], capture_output=True, text=True,
                           timeout=300, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError(f"git {args[0]} failed: {exc}", 4) from exc
    if r.returncode:
        if check:
            raise SyncError(f"git {' '.join(args)} failed in {home}: {r.stderr.strip()}", 4)
        return None
    return r.stdout.strip()


def read_pin(path: Path) -> str:
    try:
        words = path.read_text(encoding="utf-8").split()
    except OSError as exc:
        raise SyncError(f"cannot read {path}: {exc}", 2) from exc
    if not words:
        raise SyncError(f"{path} is empty: write a release tag or a full commit to it", 2)
    pin = words[0]
    if re.fullmatch(r"[0-9a-fA-F]{7,39}", pin):
        raise SyncError(f"{path}: {pin!r} looks like a short commit; write all 40 characters", 2)
    if not SHA.fullmatch(pin.lower()) and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", pin):
        raise SyncError(f"{path}: {pin!r} is neither a tag nor a commit", 2)
    return pin.lower() if SHA.fullmatch(pin.lower()) else pin


def is_checkout(home: Path) -> bool:
    return (home / ".git").exists()


def check_folder(home: Path) -> None:
    if home.resolve() == REPO:
        raise SyncError("the framework checkout cannot be this repo; pass --home <another folder>", 2)
    if home.exists() and not is_checkout(home) and any(home.iterdir()):
        raise SyncError(f"{home} exists and is not a git checkout; refusing to touch it "
                        "(pass --home to use another folder)", 3)
    if is_checkout(home) and local_changes(home):
        raise SyncError(f"{home} has local changes ({local_changes(home)}). It is a read-only copy "
                        "of the framework: move or discard those changes, then run this again. To "
                        "report a framework bug, open an issue instead of patching the checkout.", 3)


def local_changes(home: Path) -> str:
    """The first few changed or untracked (not ignored) paths, or '' when the checkout is clean."""
    if head(home) is None:                     # cloned, nothing checked out yet: nothing local
        return ""
    out = git(home, "status", "--porcelain", "--untracked-files=all", check=False) or ""
    paths = [line.split(None, 1)[1] for line in out.splitlines() if line.strip()]
    return ", ".join(paths[:5]) + (", ..." if len(paths) > 5 else "")


def problem(home: Path, commit: str | None) -> str | None:
    """Why the checkout is not exactly the read-only pin, or None: it must exist, be at the pin's
    commit with a detached HEAD, and have no changed or untracked (not ignored) files."""
    at = head(home)
    if at is None:
        return "there is no framework checkout"
    if commit is None or at != commit:
        return f"it is at {at[:12]}, not at the pin"
    if git(home, "symbolic-ref", "--quiet", "HEAD", check=False) is not None:
        return "it is on a branch, not at a detached HEAD"
    changed = local_changes(home)
    if changed:
        return f"it has local changes: {changed}"
    return None


def resolve(home: Path, pin: str, fetch: bool) -> str | None:
    """The commit the pin names, fetching it when it is not local and ``fetch`` is set."""
    if SHA.fullmatch(pin):
        if git(home, "cat-file", "-e", pin + "^{commit}", check=False) is None:
            if not fetch:
                return None
            git(home, "fetch", "--quiet", "--no-tags", "origin", pin)
        return pin
    ref = "refs/tags/" + pin
    found = git(home, "rev-parse", "--quiet", "--verify", ref + "^{commit}", check=False)
    if found is None and fetch:
        if git(home, "fetch", "--quiet", "--no-tags", "origin", f"+{ref}:{ref}", check=False) is None:
            branch = git(home, "ls-remote", "--heads", "origin", pin, check=False)
            if branch:
                raise SyncError(f"{pin!r} is a branch, and a branch moves. Pin a release tag or a "
                                "full commit.", 2)
            raise SyncError(f"the framework repository has no tag {pin!r}", 2)
        found = git(home, "rev-parse", "--quiet", "--verify", ref + "^{commit}")
    return found


def head(home: Path) -> str | None:
    if not is_checkout(home) or not git(home, "ls-files", check=False):
        return None
    return git(home, "rev-parse", "HEAD", check=False)


def this_venv() -> Path | None:
    """The venv this Python runs in, or None when it is not in one."""
    return Path(sys.prefix) if sys.prefix != sys.base_prefix else None


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install_stamp(venv: Path | None = None) -> Path:
    """Kept inside the venv, so a venv that is deleted or recreated takes its record with it."""
    return (venv or Path(sys.prefix)) / "framework-sync.json"


def cli_installed(venv: Path | None = None) -> bool:
    bindir = (venv or Path(sys.prefix)) / ("Scripts" if os.name == "nt" else "bin")
    return any((bindir / name).is_file() for name in ("striim-test", "striim-test.exe"))


def requires_python(home: Path) -> tuple[int, ...] | None:
    """The lowest Python the checkout supports: the ``>=`` bound of requires-python in its
    pyproject.toml, or None when there is none (pip still enforces whatever the checkout says)."""
    try:
        text = (home / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"""^requires-python\s*=\s*["'][^"']*>=\s*([0-9]+(?:\.[0-9]+){0,2})""", text, re.M)
    if not m:
        return None
    parts = [int(v) for v in m.group(1).split(".")]
    return tuple(parts + [0] * (2 - len(parts)))


def python_version(python: str | Path) -> tuple[int, ...] | None:
    """The version of the Python at ``python``, or None when it does not run."""
    try:
        r = subprocess.run([str(python), "-c", "import sys; print(*sys.version_info[:3])"],
                           capture_output=True, text=True, timeout=60)
        return tuple(int(v) for v in r.stdout.split()) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def dotted(version) -> str:
    return ".".join(str(v) for v in version)


def find_python(need: tuple[int, ...] | None, chosen: str | None) -> str:
    """A Python of at least ``need`` to make the venv with: ``chosen`` (--python) when given, else
    the Python running this, else the first python3.N on PATH from ``need`` up, else what uv finds."""
    if chosen:
        have = python_version(chosen)
        if have is None:
            raise SyncError(f"--python {chosen} does not run", 2)
        if need and have < need:
            raise SyncError(f"--python {chosen} is Python {dotted(have)}; the framework needs "
                            f"{dotted(need)} or later", 2)
        return chosen
    if not need or tuple(sys.version_info[:3]) >= need:
        return sys.executable
    candidates = [shutil.which(f"python{need[0]}.{minor}") for minor in range(need[1], need[1] + 10)]
    uv = shutil.which("uv")
    if uv:
        try:
            r = subprocess.run([uv, "python", "find", "--system", "--no-python-downloads", ">=" + dotted(need)],
                               capture_output=True, text=True, timeout=60)
            candidates.append(r.stdout.strip() if r.returncode == 0 else None)
        except (OSError, subprocess.TimeoutExpired):
            pass
    for candidate in filter(None, candidates):
        have = python_version(candidate)
        if have and have >= need:
            return candidate
    raise SyncError(f"the framework needs Python {dotted(need)} or later. This is Python "
                    f"{dotted(sys.version_info[:3])}, and no python{need[0]}.N on your PATH is new enough. "
                    f"Install Python {dotted(need)} or later (from python.org, Homebrew, your package "
                    f"manager, or `uv python install {dotted(need)}`), then run this again, or pass "
                    "--python with the path of one", 2)


def prepare_venv(venv: Path, need: tuple[int, ...] | None, chosen: str | None, pin: str) -> str | None:
    """Check that ``venv`` has a Python of at least ``need``, or make it when it does not exist.
    Returns the Python it made the venv with, or None when the venv was already there."""
    if venv.exists():
        ours = venv == this_venv()
        have = tuple(sys.version_info[:3]) if ours else python_version(venv_python(venv))
        if have is None:
            raise SyncError(f"{venv} exists but is not a working venv: remove it and run this again", 3)
        if need and have < need:
            fix = (f"remove it (rm -rf {venv}) and run python3 scripts/sync-framework.py again; it makes "
                   "a new one with a Python that qualifies" if venv.resolve() == (REPO / ".venv").resolve()
                   else "use a venv made from a newer Python, or run python3 scripts/sync-framework.py "
                   "outside any venv and it makes this repo's .venv")
            raise SyncError(f"{venv} has Python {dotted(have)}, and the framework at {pin} needs "
                            f"Python {dotted(need)} or later: {fix}", 2)
        return None
    base = find_python(need, chosen)
    r = subprocess.run([base, "-m", "venv", str(venv)], capture_output=True, text=True)
    if r.returncode:
        raise SyncError(f"could not make {venv} with {base}: {(r.stderr or r.stdout).strip()}", 5)
    return base


def make_env_file() -> bool:
    """Copy .env.example to .env, readable only by you, when .env is missing. True when it did."""
    env, example = REPO / ".env", REPO / ".env.example"
    if env.exists() or not example.is_file():
        return False
    with os.fdopen(os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
        f.write(example.read_text(encoding="utf-8"))
    return True


def install(home: Path, commit: str, run=subprocess.run, find_links: Path | None = None,
            offline: bool = False, venv: Path | None = None) -> bool:
    """pip install -e the checkout into ``venv`` (default: this Python's venv), unless the venv's own
    record says it already holds ``commit`` from ``home`` and its `striim-test` is there. True when it
    installed.

    Online, pip fetches the framework's dependencies and its build backend (setuptools) from your
    package index. ``find_links`` installs them from a local folder of wheels instead, with no index;
    ``offline`` uses only what the venv already has (no index, no isolated build environment)."""
    venv = venv or this_venv()
    if venv is None:
        raise SyncError("no venv to install into: run python3 scripts/sync-framework.py outside a venv "
                        "and it makes this repo's .venv, or pass --no-install", 2)
    python = sys.executable if venv == this_venv() else str(venv_python(venv))
    stamp = install_stamp(venv)
    want = {"commit": commit, "home": str(home)}
    try:
        if json.loads(stamp.read_text()) == want and cli_installed(venv):
            return False
    except (OSError, ValueError):
        pass
    argv = [python, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    if find_links is not None:
        argv += ["--no-index", "--find-links", str(find_links)]
    if offline:
        argv += ["--no-index", "--no-build-isolation"]
    r = run(argv + ["-e", str(home)])
    if r.returncode:
        raise SyncError("pip install of the framework failed (see the output above)"
                        + ("" if find_links or offline else "; without a package index, see --find-links"), 5)
    stamp.write_text(json.dumps(want))
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--pin", type=Path, default=REPO / "framework.pin", help="the pin file")
    p.add_argument("--home", type=Path, default=Path(os.environ.get("FRAMEWORK_CHECKOUT") or DEFAULT_HOME),
                   help="the framework checkout (default: next to this repo, or $FRAMEWORK_CHECKOUT)")
    p.add_argument("--remote", default=os.environ.get("FRAMEWORK_REMOTE") or DEFAULT_REMOTE,
                   help="where to clone the framework from (or $FRAMEWORK_REMOTE)")
    p.add_argument("--check", action="store_true", help="change nothing; exit 1 unless at the pin")
    p.add_argument("--dry-run", action="store_true", help="change nothing; say what would happen")
    p.add_argument("--no-install", action="store_true", help="do not pip install the checkout")
    p.add_argument("--find-links", type=Path, metavar="DIR",
                   help="install the dependencies from this folder of wheels, with no package index")
    p.add_argument("--offline", action="store_true",
                   help="install with no package index and no isolated build: every dependency, and "
                        "setuptools, must already be in the venv")
    p.add_argument("--python", metavar="PATH",
                   help="the Python to make .venv with, when there is no venv yet (default: the first "
                        "one found that the pinned framework supports)")
    args = p.parse_args(argv)
    home = args.home.expanduser().absolute()
    venv = this_venv() or REPO / ".venv"
    try:
        pin = read_pin(args.pin)
        if args.check:
            commit = resolve(home, pin, fetch=False) if is_checkout(home) else None
            why = problem(home, commit)
            if why is None:
                print(f"framework: {home} is at {pin}, clean and detached")
                return 0
            print(f"framework: {home} is not at {pin}: {why}. Run scripts/sync-framework.py "
                  "(it refuses local changes: move or discard them first)", file=sys.stderr)
            return 1
        if args.dry_run:
            action = "update" if is_checkout(home) else f"clone {args.remote} into"
            print(f"framework: would {action} {home}, check out {pin}"
                  + ("" if args.no_install else f", and install it into {venv}"
                     + ("" if venv.exists() else ", making it first")))
            if not (REPO / ".env").exists() and (REPO / ".env.example").is_file():
                print("settings: would copy .env.example to .env")
            return 0
        check_folder(home)
        if not is_checkout(home):
            home.parent.mkdir(parents=True, exist_ok=True)
            git(home.parent, "clone", "--quiet", "--no-checkout", args.remote, str(home))
        commit = resolve(home, pin, fetch=True)
        git(home, "checkout", "--quiet", "--detach", commit)      # also leaves a branch at the pin
        why = problem(home, commit)
        if why is not None:
            raise SyncError(f"{home} is not at the pin after checkout: {why}", 3)
        installed = False
        if not args.no_install:
            made_with = prepare_venv(venv, requires_python(home), args.python, pin)
            if made_with:
                print(f"python: made {venv} with {made_with} "
                      f"(Python {dotted(python_version(venv_python(venv)) or ('?',))})")
            installed = install(home, commit, find_links=args.find_links, offline=args.offline, venv=venv)
        print(f"framework: {home} at {pin} ({commit[:12]})" + (f", installed into {venv}" if installed else ""))
        if make_env_file():
            print(f"settings: made {REPO / '.env'} from .env.example. Open it and set where Striim comes "
                  "from before you run a test (`ls -a` shows these dot files)")
        if not args.no_install and (REPO / "gold-targets.yaml").is_file():
            bin_dir = venv_python(venv).parent
            cli = bin_dir.relative_to(REPO) if REPO in bin_dir.parents else bin_dir
            print(f"next, from {REPO}:\n  {cli / 'striim-test'} list --targets gold-targets.yaml\n"
                  f"  {cli / 'striim-test'} doctor --targets gold-targets.yaml --case tests/live\n"
                  f"  {cli / 'striim-test'} run --targets gold-targets.yaml")
        return 0
    except SyncError as exc:
        print(f"sync-framework: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
