from __future__ import annotations
import glob
import os
import re
import subprocess
from pathlib import Path

from livetest import stack

# Read/clear a target's output FILE on the Striim server, mode-aware. A FileWriter
# writes to the server's local filesystem and adds a rollover sequence to the declared name:
# before the extension, split at the name's FIRST dot (5.4.2 RolloverFilenameFormat:
# rows.json -> rows.00.json, rows.a.json -> rows.00.a.json), or appended when there is no
# extension (out -> out.00). _output_names selects those files and nothing else. Docker: the
# single-topology app runs on the `default` group (primary or node), so read/clear on
# BOTH cluster nodes and join the files (see _join_files). Native: the local filesystem.

# Base names; every docker call below resolves them through stack.app_nodes() at call time
# so a prefixed stack (SLT_STACK_PREFIX) reads/places files on ITS OWN nodes.
_CLUSTER_NODES = stack.APP_NODES
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+\Z")   # author-controlled path; keep it shell-safe.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+\Z")     # a bare identifier: no "/" and no ".." possible.
# \Z (not $): $ also matches just before a trailing "\n", which would let a newline slip
# into the f-string shell command; \Z anchors at the true end of string.


class StriimFileError(Exception):
    pass


def _check(path: str) -> str:
    if not _SAFE_PATH.match(path):
        raise StriimFileError(f"unsafe file path {path!r} (allowed: letters, digits, _ . / -)")
    return path


def _output_names(base: str, names) -> list[str]:
    # The names among `names` that are FileWriter output for the declared basename `base`:
    # the name itself, <stem>.<n><.rest> (sequence before the extension, first-dot split) and
    # <base>.<n> (no extension, or the sequence appended after it). Numeric order, deduplicated.
    stem, dot, rest = base.partition(".")
    shapes = [re.escape(base) + r"\.(\d+)"]
    if dot:
        shapes.append(re.escape(stem) + r"\.(\d+)\." + re.escape(rest))
    # fullmatch: the whole name must be one of the shapes -- anchoring only the last
    # alternative let rows.json.00.bak or rows.json.01-other-run through.
    rollover = re.compile("|".join(f"(?:{p})" for p in shapes))
    keyed = {}
    for n in names:
        if n == base:
            keyed[n] = (-1, n)
            continue
        m = rollover.fullmatch(n)
        if m:
            keyed[n] = (int(next(g for g in m.groups() if g is not None)), n)
    return sorted(keyed, key=keyed.get)


def _fail(what: str, node: str, r) -> StriimFileError:
    return StriimFileError(
        f"{what} on {node} failed (exit {getattr(r, 'returncode', '?')}): "
        f"{(getattr(r, 'stderr', '') or '').strip()}")


def _docker_output_files(run, node: str, path: str) -> list[str]:
    # A missing directory is no output yet; any other listing failure (missing container,
    # unsearchable ancestor, unreadable directory) raises rather than passing for zero rows.
    # `[ -d ]` alone is false for both absent and unreachable, so ask ls which one it is.
    parent, base = os.path.split(path)
    parent = parent or "."
    r = run(["docker", "exec", node, "sh", "-c",
             f"if [ ! -d {parent} ]; then err=$(LC_ALL=C ls -d {parent} 2>&1) || "
             f"case \"$err\" in *'No such file'*) exit 0;; *) echo \"$err\" >&2; exit 1;; esac; fi; "
             f"find {parent} -mindepth 1 -maxdepth 1 -type f"])
    if getattr(r, "returncode", 0) != 0:
        raise _fail(f"listing {parent}", node, r)
    names = [os.path.basename(line) for line in (getattr(r, "stdout", "") or "").splitlines()
             if os.path.dirname(line) == parent]
    return [os.path.join(parent, n) for n in _output_names(base, names)]


def _native_output_files(path: str) -> list[str]:
    parent, base = os.path.split(path)
    parent = parent or "."
    try:
        with os.scandir(parent) as it:
            names = [e.name for e in it if e.is_file()]
    except FileNotFoundError:
        return []
    except OSError as e:
        raise StriimFileError(f"listing {parent} failed: {e}") from e
    return [os.path.join(parent, n) for n in _output_names(base, names)]


_CAT_EACH = 'for f in "$@"; do cat -- "$f" || exit 1; printf "\\0"; done'


def _join_files(parts) -> str:
    # One file after another, with a newline between two files when the first does not end in
    # one. FileWriter's JSONFormatter (EventsAsArrayOfJsonObjects false) separates records with
    # a newline but ends each file without one, so a plain concatenation fused the last record
    # of one rollover file with the first of the next and every line count came up one short
    # per rollover. Nothing is added after the last file, so a single file reads back exactly.
    out = []
    for part in parts:
        if not part:
            continue
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        out.append(part)
    return "".join(out)


def read_server_files(ctx, path: str, run=None) -> str:
    # Content of <path>'s FileWriter output (see _output_names) from the Striim server, file by
    # file and node by node (see _join_files); "" when none exists yet. A failed listing or read
    # raises StriimFileError.
    _check(path)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    if ctx.mode == "docker":
        parts = []
        for node in stack.app_nodes():
            files = _docker_output_files(run, node, path)
            if not files:
                continue
            # One exec per node; the paths travel as arguments, and a NUL after each file marks
            # where it ends (text FileWriter output contains no NUL).
            r = run(["docker", "exec", node, "sh", "-c", _CAT_EACH, "sh", *files])
            if getattr(r, "returncode", 0) != 0:
                raise _fail(f"reading {' '.join(files)}", node, r)
            parts.extend((getattr(r, "stdout", "") or "").split("\0")[:-1])
        return _join_files(parts)
    parts = []
    for f in _native_output_files(path):
        try:
            parts.append(Path(f).read_text())
        except FileNotFoundError:
            pass
        except OSError as e:
            raise StriimFileError(f"reading {f} failed: {e}") from e
    return _join_files(parts)


def clear_server_dir(ctx, dest: str, run=None) -> None:
    # Empty the DIRECTORY holding `dest` on the server, so a re-run's reader does not pick up a
    # previous run's input. The counterpart of clear_server_files, which does this for a
    # FileWriter's output; a FileReader tailing a directory has the same problem in reverse, and
    # worse — it re-reads whatever it finds, including a fixture that has since been renamed.
    #
    # Safe to empty rather than delete selectively because the directory is the framework's own:
    # a server_files `dest` MUST carry ${NS} or ${TID} (the isolation suite enforces it), so the
    # path is test-scoped, and ensure_server_dir created it. Non-recursive, so a nested directory
    # is left alone. Best-effort, like the output side.
    _check(dest)
    parent = str(Path(dest).parent)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    if ctx.mode == "docker":
        for node in stack.app_nodes():
            run(["docker", "exec", node, "sh", "-c", f"rm -f {parent}/*"])
    else:
        for f in glob.glob(parent + "/*"):
            try:
                if Path(f).is_file():
                    Path(f).unlink()
            except OSError:
                pass


def ensure_server_dir(ctx, dest: str, run=None) -> None:
    # mkdir -p the parent directory of `dest` on the server (so a FileReader watching it
    # sees the dir at deploy, even when the file itself is dropped later / post_start).
    _check(dest)
    parent = str(Path(dest).parent)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    if ctx.mode == "docker":
        for node in stack.app_nodes():
            run(["docker", "exec", node, "mkdir", "-p", parent])
    else:
        Path(parent).mkdir(parents=True, exist_ok=True)


def place_server_file(ctx, src, dest: str, run=None) -> None:
    # Copy a local file `src` to `dest` on the server (both cluster nodes for docker, or
    # the local fs for native). Creates the parent dir first.
    _check(dest)
    ensure_server_dir(ctx, dest, run=run)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    if ctx.mode == "docker":
        for node in stack.app_nodes():
            r = run(["docker", "cp", str(src), f"{node}:{dest}"])
            if getattr(r, "returncode", 1) != 0:
                raise StriimFileError(
                    f"docker cp {src} -> {node}:{dest} failed: {getattr(r, 'stderr', '')}")
    else:
        import shutil
        shutil.copy(str(src), dest)


def clear_server_files(ctx, path: str, run=None) -> None:
    # Remove <path>'s FileWriter output (the same files read_server_files reads) on the
    # server so a re-run's assertion doesn't see a prior run's output (FileWriter appends
    # across runs). Nothing to remove is fine; a failed listing or removal raises.
    _check(path)
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    if ctx.mode == "docker":
        for node in stack.app_nodes():
            files = _docker_output_files(run, node, path)
            if not files:
                continue
            r = run(["docker", "exec", node, "sh", "-c", "rm -f -- " + " ".join(files)])
            if getattr(r, "returncode", 0) != 0:
                raise _fail(f"removing {' '.join(files)}", node, r)
    else:
        for f in _native_output_files(path):
            try:
                Path(f).unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                raise StriimFileError(f"removing {f} failed: {e}") from e


# OP-written resume checkpoints, and why the framework has to delete them.
#
# A source OP that resumes where it left off persists its position beside Striim's working
# directory: one such OP writes `<prefix>_<hash>_<app>__<component>.position.json`
# (+ `.position.bk`), another writes `.cursors.json`/`.cursors.bk`. Both embed
# the app-qualified name, so a live test's files carry its `SLT_<test>` namespace.
#
# Those files outlive the app, the namespace, and the run -- which is correct in production
# and wrong for a test. The next run's same-named app finds a checkpoint from the PREVIOUS
# run and resumes from it. For a Spanner change-stream reader that is fatal rather than merely stale: a
# Spanner change stream only retains a limited window, so a position written 40 minutes ago
# is already outside it and the first partition query dies with
#     OUT_OF_RANGE: Specified start_timestamp is too far in the past
# taking the app to TERMINATED. Measured: two back-to-back full suite runs against one
# cluster, 0 such errors in the first and 18 in the second.
#
# The OP is right to fail there -- an aged-out position means change-stream data really was
# missed, and skipping ahead silently would hide data loss. So the fix belongs here: a test
# must not inherit a previous run's position at all. Cleared at SETUP rather than teardown,
# matching clear_server_files: teardown does not run when a run is killed, and setup-time
# cleanup also repairs a cluster someone else left dirty.
_CHECKPOINT_SUFFIXES = (".position.json", ".position.bk", ".cursors.json", ".cursors.bk")

# The un-namespaced fallback bases. PositionCheckpointStore.resolveBaseName degrades to the
# bare prefix when neither the app nor the component name is resolvable, so a run can leave
# a bare `<prefix>.position.json` behind -- a file no namespace-scoped glob can ever match,
# and restore() reads whatever name it resolved. That is precisely a file that reproduces the
# aged-out-position failure, so sweep it too. The cursors-file equivalent never gets READ, but it
# is the same litter. Recognised by shape, not by a list of OP names: a bare prefix is letters and
# digits only, while every namespaced name carries the namespace's "_" and ".".
_BARE_CHECKPOINT_SH = ('for f in {globs}; do b=${{f##*/}}; '
                       'case "${{b%%.*}}" in ""|*[!A-Za-z0-9]*) ;; *) rm -f "$f";; esac; done')


def clear_op_checkpoints(ctx, namespace: str, run=None, suffixes=_CHECKPOINT_SUFFIXES) -> None:
    """Delete OP resume checkpoints belonging to `namespace` from Striim's working dir.

    DOCKER ONLY, deliberately. `ctx.mode == "native"` does not mean "a local install at
    STRIIM_HOME" -- it means "a reachable Striim that is not our compose cluster", which
    includes a REMOTE server (see README, running against a native or remote Striim). Deleting
    local files then would leave the real server's checkpoints untouched -- the bug unfixed --
    while running `rm` over a directory never established to be the server's. Even for a
    genuinely local install both OPs resolve the checkpoint dir as `System.getProperty(
    "user.dir")`, which equals $STRIIM_HOME only if the server was launched from there. So
    this cleans what it can actually see and no-ops otherwise.

    Scoped to the namespace AND ANCHORED ON THE DOT that follows it. A bare `*<ns>*` substring
    match also matches every namespace having this one as a PREFIX, and those exist:
    `reader-streaming-to-file` vs `-to-file-pg` (plus the `-v2`/`-v3` members of a
    suite, and ~30 more). Under SLT_PARALLEL that meant one test's
    setup deleting a sibling's LIVE checkpoint and its crash-safety backup while that
    sibling's OP was still writing them -- the exact hazard this scoping exists to prevent.
    CheckpointNaming.sanitize preserves ".", and the filename always contains
    `<namespace>.<app>` / `<namespace>.<component>`, so anchoring on `<ns>.` is exact.

    Suffixes are globbed with a trailing `*` so the `.tmp` files both stores write during an
    atomic persist are swept too; neither store reads a `.tmp` on restore, so those are litter
    rather than a stale resume, but they are server-side OP state and otherwise accumulate
    forever.

    Two known gaps, recorded rather than guessed at:
      * A namespace longer than CheckpointNaming.MAX_NAME_CHARS (60) is truncated OUT of the
        filename, so this would silently match nothing. runident caps the namespace at 40
        (NS_MAX), so a framework-derived one never is.
      * A reader's `CheckpointDirectory` property can move the files out of the
        working dir entirely. No live test sets it; the first one that does gets no cleanup.

    Best-effort, exactly like clear_server_files: a cluster that cannot be reached is not a
    reason to fail a test that has not started.
    """
    # Stricter than _check: this builds an `rm -f` glob, so a namespace must be a plain
    # identifier. _SAFE_PATH permits "/" and "." (it validates PATHS), which would let a
    # namespace containing ".." or a slash reach outside the checkpoint directory. Namespaces
    # are framework-generated ("SLT_" + slug) and can never contain either, so this only ever
    # fires on a caller bug -- which is exactly when you want it to.
    if not _SAFE_NAME.match(namespace):
        raise StriimFileError(
            f"unsafe namespace {namespace!r} for checkpoint cleanup "
            f"(allowed: letters, digits, _ -)")
    if getattr(ctx, "mode", None) != "docker":
        return
    run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
    pattern = " ".join(f"{_DOCKER_HOME}/*{namespace}.*{suffix}*" for suffix in suffixes)
    bare = _BARE_CHECKPOINT_SH.format(globs=" ".join(f"{_DOCKER_HOME}/*{suffix}*" for suffix in suffixes))
    for node in stack.app_nodes():
        run(["docker", "exec", node, "sh", "-c", f"rm -f {pattern}; {bare}"])


# Striim's install root inside the compose containers, where an OP's relative checkpoint path
# resolves. A constant, never interpolated from caller input.
_DOCKER_HOME = "/opt/striim"
