"""Cross-worker OP-registration registry for parallel live runs (spec §B.4, CLI xdist path).

pytest-xdist gives no controller pre-run hook for arbitrary code, so N worker processes
coordinate through a shared JSON file guarded by a filelock: the first worker to need a
given jar+fingerprint registers it (LOAD OPEN PROCESSOR / load_jar) and records it; every
other worker sees the record and skips. Workers NEVER unregister -- the union stays loaded
until the janitor clears it (spec §B.4's documented asymmetry). Serial runs used to clear it
implicitly via a per-test unload+load; that path is gone, so BOTH modes now go through
ensure_registered. A console pre-flight (Phase 3) bypasses this entirely by setting
SLT_OPS_PRELOADED=1.

The registry SURVIVES ACROSS RUNS, and that is the point. It used to be deleted at every
session start, because a record kept on the host cannot see a `docker compose down -v` that
destroyed the loaded jar inside the container -- but wiping it meant the content-hash diff
never spanned runs, so every run re-registered every OP jar (an UNLOAD + LOAD OPEN PROCESSOR
of cluster-wide state) whether or not a byte had moved. That reload is what wedges Striim's
OP loader, and nothing asked for it.

The stale-record hazard is closed by ASKING instead: ensure_registered takes a `verify`
callback, and plugin._loaded_jar_probe answers it from LIST LIBRARIES -- keyed on the jar
filename, which is exactly what LOAD/UNLOAD operate on and what these records hold, and which
covers UDF jars as well as OP jars.

What LIST LIBRARIES cannot see is a JVM restart: the library registration lives in the MDR,
inside a volume that survives one, while the class loaders it built do not. So the CALLER
folds striim_provision.app_nodes_generation() into the key, which changes whenever an app
node has restarted, and restart_app_nodes additionally calls clear() for the restarts the
framework performs itself.
"""
from __future__ import annotations
import hashlib
import json
import os
import uuid
from pathlib import Path

import fcntl
import time
from contextlib import contextmanager

from filelock import FileLock

from livetest import paths
from livetest import lockdir, stack

_REGISTRY = ".slt-op-registry.json"
_LOCK = ".slt-op-registry.lock"
_USE_LOCK = ".slt-op-inuse.lock"


def cluster_tag(env=None) -> str:
    """Short, stable tag for the CLUSTER these records describe, or "" for the compose
    default.

    SLT_STACK_PREFIX identifies the cluster whenever the cluster IS this checkout's compose
    stack: compose.yaml prefixes both the project name and every container_name, so two
    stacks at one prefix collide in docker before any record is read -- prefix and cluster
    are the same thing there, which is why prefix-scoping alone is sufficient for the
    docker case.

    It stops being sufficient the moment the target is NOT that stack. Two checkouts with
    no prefix pointed at different Striims -- one at the compose stack and one at a native
    install or a remote server via STRIIM_URL -- are two clusters at one prefix, and a
    prefix-only name would hand them a single shared record. The failure mode is the
    dangerous direction: the second checkout reads the first's record and SKIPS a load its
    own server never had, which is the "loaded? no" state that ends in a wedged cluster.

    So the address participates in the name. Unset STRIIM_URL keeps the historical
    (prefix-only) filename, so the common path is byte-identical to before and no existing
    record is orphaned.
    """
    e = env if env is not None else paths.effective_env()
    resolved = _resolved_url(e)
    # The DEFAULT cluster contributes no tag, so its filenames stay exactly what they were
    # before this existed. That is back-compat, but it is also correctness: the framework
    # DERIVES this same URL when STRIIM_URL is unset (plugin.py), so a checkout that sets
    # STRIIM_URL explicitly to the value it would have derived anyway is pointed at the very
    # same cluster and must land on the SAME record. Hashing the raw variable instead of the
    # resolved one split those two apart and silently un-shared them -- which is the exact
    # sharing this module exists to provide.
    if resolved == _resolved_url(e, ignore_url=True):
        return ""
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]


def _resolved_url(e, ignore_url: bool = False) -> str:
    """The Striim URL this environment actually resolves to, normalised.

    Mirrors plugin.py's resolution: STRIIM_URL wins; otherwise the compose default on
    SLT_SERVICES_HOST (or localhost). `ignore_url` computes what the DEFAULT would be for
    the same host, which is what makes an explicit-but-default STRIIM_URL compare equal.
    """
    host = (e.get("SLT_SERVICES_HOST") or "").strip() or "localhost"
    if not ignore_url:
        url = (e.get("STRIIM_URL") or "").strip()
        if url:
            return url.rstrip("/")
    return f"http://{host}:9080"


def _scoped(base: str, env=None) -> str:
    """`base`, scoped by stack prefix AND cluster address. The tag goes before the
    extension so the file keeps its .json/.lock suffix (and its leading dot)."""
    name = stack.state_name(base, env)
    tag = cluster_tag(env)
    if not tag:
        return name
    stem, dot, ext = name.rpartition(".")
    return f"{stem}-{tag}{dot}{ext}" if dot else f"{name}-{tag}"


def registry_path(registry_dir=None, env=None) -> Path:
    """Machine-wide by default (stack.lock_path), matching `_use_lock` below.
    `registry_dir` overrides it -- tests inject a tmp_path to get an isolated registry;
    production callers pass nothing.

    Prefix-scoped per stack (SLT_STACK_PREFIX) either way: parallel stacks register
    against different clusters, so their records must not mix.

    It used to live in the CHECKOUT (scripts/live), which made the registry per-checkout
    while the cluster it describes is machine-wide -- the in-use lock beside it was
    already machine-wide for exactly that reason. Two agents in separate checkouts or
    worktrees pointed at one cluster therefore each kept their own view of what was
    loaded, so the second one redundantly re-ran UNLOAD + LOAD OPEN PROCESSOR for a jar
    already loaded at the identical fingerprint. The reader-writer lock keeps that
    reload from landing mid-run, so it is not a correctness bug -- but it is a
    cluster-wide reload nobody needed, and `LOAD OPEN PROCESSOR` replaces the artifact
    for every app on the server. One shared record removes it entirely.

    NOTE this does not (and cannot) help two checkouts on DIFFERENT BRANCHES: they
    produce genuinely different bytes for one jar name, so the fingerprints differ and
    each legitimately needs its own load. Only one build of a package can be loaded at a
    time, so that case wants separate clusters (SLT_STACK_PREFIX plus per-stack
    *_HOST_PORT overrides), not a shared registry.
    """
    if registry_dir is not None:
        return Path(registry_dir) / _scoped(_REGISTRY, env)
    return stack.lock_path(_REGISTRY, env).parent / _scoped(_REGISTRY, env)


def clear(registry_dir=None) -> None:
    """Remove the registry JSON (best-effort).

    Called by striim_provision.restart_app_nodes: restarting the nodes throws away every
    in-JVM class loader while the MDR keeps the library registrations, so the cluster would
    still answer "loaded" for a jar it can no longer instantiate. Every record is stale at
    that moment.

    NOT the only such event -- a host reboot, a `compose stop/start`, a daemon restart or an
    OOM kill do the same -- which is why the registry KEY also carries
    app_nodes_generation(). This call is the immediate, explicit half for the restarts the
    framework performs itself.

    It is NOT called at session start any more -- that wipe is what made the content-hash
    diff useless across runs (see the module docstring).

    Takes the SAME lock ensure_registered writes under. Without it the unlink raced a
    registration already in flight: that caller has the whole state dict in memory and
    rewrites it wholesale at the end, so it would re-create the file with every pre-restart
    record intact -- silently undoing the wipe, and leaving records the probe then CONFIRMS
    (LIST LIBRARIES still lists them, since the MDR outlives the restart) for jars whose class
    loaders are gone.

    Clearing is always the SAFE direction: it can only cause a redundant re-registration,
    never a skipped load."""
    with FileLock(str(stack.lock_path(_LOCK).parent / _scoped(_LOCK)), mode=lockdir.file_mode()):
        _unlink(registry_path(registry_dir))


def record(jar_name: str, fingerprint: str, registry_dir=None) -> None:
    """Record that `jar_name` is loaded at `fingerprint`, WITHOUT running a registration.

    For a caller that has just loaded the jar by another route and would otherwise leave no
    trace -- the poison-recovery path, which reloads its OP jars directly after restarting the
    nodes. Without this the next test finds no record, re-registers, and hits "already been
    loaded", so it performs a second full UNLOAD + LOAD on a loader that was only just
    recovered from poisoning.

    Clears any remembered failure for the jar, since it has demonstrably loaded.
    """
    reg_path = registry_path(registry_dir)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(stack.lock_path(_LOCK).parent / _scoped(_LOCK)), mode=lockdir.file_mode()):
        try:
            state = json.loads(reg_path.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        failed = state.get(_FAILED) if isinstance(state.get(_FAILED), dict) else {}
        failed.pop(jar_name, None)
        if failed:
            state[_FAILED] = failed
        else:
            state.pop(_FAILED, None)
        state[jar_name] = fingerprint
        lockdir.write_text(reg_path, json.dumps(state))


def _unlink(path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except PermissionError:     # another user's file in the shared sticky dir: empty it instead
        lockdir.write_text(path, "{}")


# Remembered failures live under this reserved key rather than in the jar->fingerprint map,
# so a failure is never mistaken for a registration and the map keeps its historical shape.
# Jar names always end in ".jar", so they cannot collide with it.
_FAILED = "#failed"

# Set to ignore the remembered-failure memory (for as long as it is set).
_RETRY_ENV = "SLT_OP_RETRY_FAILED"

# How many failures at the SAME bytes, in the same run, before we stop retrying. Two, not
# one: a single LockTimeout from one worker waiting out a slow drain must not fail every
# remaining test for that jar without any of them trying.
_FAILURES_BEFORE_GIVING_UP = 2


class RegistrationFailed(RuntimeError):
    """Raised in place of re-running a registration that already failed at these bytes."""


def _truthy(value) -> bool:
    """Env-var truthiness. A bare `bool(os.environ.get(...))` reads "0"/"false" as ON, which
    for a flag that DISABLES a safety memory is the wrong way to be wrong."""
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def _current_run(env=None) -> str:
    """The run this process belongs to, for scoping remembered failures.

    Stamps a per-process token when SLT_RUN_EPOCH is unset -- NOT a shared constant. A constant would match forever across every
    future run, which is precisely the permanent brick that run-scoping exists to avoid, and
    it would apply to any caller reached without the stamp: an ad-hoc script, a new non-pytest
    entry point, or a path that registers under SLT_OPS_PRELOADED (where pytest_configure
    deliberately does not stamp).
    """
    e = env if env is not None else os.environ
    if e is os.environ:
        return os.environ.setdefault("SLT_RUN_EPOCH", uuid.uuid4().hex[:12])
    return e.get("SLT_RUN_EPOCH") or "unknown"


def ensure_registered(registry_dir, jar_name: str, fingerprint: str,
                       do_register, verify=None, retry_failed: bool = False) -> bool:
    """Register jar_name (via do_register()) exactly once across processes, keyed by
    (jar_name, fingerprint). Returns True if THIS call performed the registration, False if
    a prior call/worker already had it at the same fingerprint.

    `verify` (optional, zero-arg) is consulted ONLY when the record already matches this
    fingerprint, to confirm the cluster still backs it: True skips the registration, False
    re-registers, and None ("cannot tell") trusts the record. It never authorises a skip on
    its own -- without a record we do not know WHICH bytes the server holds, and "something
    is loaded under this name" is not the same claim.

    That is what lets the registry survive across runs. It used to be deleted at the start of
    every session, because a record kept on the host cannot see a `compose down -v` that
    destroyed the jar inside the container; clearing was the safe direction but it made the
    content-hash diff useless run-to-run, so every run re-registered every OP jar whether or
    not a byte had changed. Asking the cluster answers that question directly instead.

    A failure is REMEMBERED FOR THE REST OF THE RUN. do_register() runs before the record is
    written, so a failed registration used to leave nothing behind and every later caller
    re-attempted the same destructive reload -- one UNLOAD+LOAD per test, against a cluster
    that had already proven it would not accept the jar. The failure is now recorded against
    (these bytes, this run) and re-raised as RegistrationFailed without touching the server.

    Run-scoped deliberately, and NOT keyed on bytes alone. do_register() covers a 900s lock
    wait, a `docker cp`, and an HTTP call, so most of what can go wrong there is transient --
    lock starvation, a container mid-restart, a connection reset, a poisoned loader that a
    JVM restart clears. Remembering those forever would let one bad moment brick every test
    for that jar until someone deleted the registry by hand.

    And it takes TWO failures, not one, for the same reason: a single `LockTimeout` from one
    worker waiting out a slow drain would otherwise fail every remaining test for that jar
    without any of them trying. One retry is what usually clears a transient cause; a second
    identical failure is evidence, and from then on the storm is suppressed -- which is the
    whole point. New bytes reset the count immediately, and SLT_OP_RETRY_FAILED (or
    `retry_failed=True`) ignores the memory altogether for as long as it is set.
    """
    # registry_dir may be None -- registry_path resolves that to the machine-wide default.
    reg_path = registry_path(registry_dir)
    reg_path.parent.mkdir(parents=True, exist_ok=True)

    # Probe BEFORE taking the registry lock. verify() talks to Striim, one probe can take up to
    # four LIST LIBRARIES at plugin._PROBE_TIMEOUT each (a memo miss and an ABSENT re-probe, two
    # attempts apiece), and this FileLock has no timeout -- so probing under the lock let a
    # wedged-but-listening cluster stall every worker and every other checkout pointed at it.
    # The lock re-reads the record below, so a sibling that registered while we probed is
    # still honoured; the verdict can only be stale in the harmless direction (a redundant
    # re-register, never a skipped load).
    confirmed = None
    probed_stamp = None
    if verify is not None:
        try:
            state = json.loads(reg_path.read_text())
            probed_stamp = reg_path.stat().st_mtime_ns
        except (FileNotFoundError, ValueError, OSError):
            state = {}
        if state.get(jar_name) == fingerprint:
            confirmed = verify()

    with FileLock(str(stack.lock_path(_LOCK).parent / _scoped(_LOCK)), mode=lockdir.file_mode()):
        try:
            state = json.loads(reg_path.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        failed = state.get(_FAILED) if isinstance(state.get(_FAILED), dict) else {}
        if state.get(jar_name) == fingerprint:
            # If the file moved since we probed, the verdict is re-taken rather than
            # trusted or discarded. The record we probed against and one a sibling just
            # WROTE carry the same fingerprint string, so they are indistinguishable by
            # content: after a `down -v` every xdist worker probes False at once, the first
            # registers, and the rest would each pop that fresh record and re-register --
            # N cluster-wide reloads of one jar instead of one.
            #
            # Re-taking beats assuming in either direction. Treating a moved file as
            # "cannot tell" would skip a load whenever an unrelated jar's registration
            # happened to land in our window, which is the dangerous direction. Re-taking is
            # usually a dict lookup (the probe is memoised per process), but a memoised ABSENT
            # is re-probed, so on a wedged cluster this can hold the lock for two LIST LIBRARIES
            # at plugin._PROBE_TIMEOUT.
            try:
                moved = probed_stamp is None or reg_path.stat().st_mtime_ns != probed_stamp
            except OSError:
                moved = True
            if moved and verify is not None:
                confirmed = verify()
            if confirmed is not False:
                return False
            # The cluster no longer has it (fresh container, wiped volume): fall through and
            # register again. Drop the record first so a failure below cannot leave a record
            # claiming success.
            state.pop(jar_name, None)
        prior = failed.get(jar_name)
        forced = retry_failed or _truthy(os.environ.get(_RETRY_ENV))
        same = (isinstance(prior, dict) and prior.get("fingerprint") == fingerprint
                and prior.get("run") == _current_run())
        if same and int(prior.get("count") or 0) >= _FAILURES_BEFORE_GIVING_UP and not forced:
            raise RegistrationFailed(
                f"{jar_name} failed to register at these exact bytes "
                f"{prior.get('count')} times earlier in this run, so it was not retried: "
                f"{prior.get('error')}\n"
                f"Rebuilding the jar clears this; so does a new run. To ignore this memory "
                f"for the rest of the run, set {_RETRY_ENV}=1; or delete {reg_path}.")
        try:
            do_register()
        except Exception as e:
            failed[jar_name] = {"fingerprint": fingerprint, "error": str(e),
                                "run": _current_run(),
                                "count": (int(prior.get("count") or 0) if same else 0) + 1}
            state[_FAILED] = failed
            # A failed registration may already have changed what the cluster holds (an OP
            # registration UNLOADs the OP's other builds before its LOAD), so the old record
            # must not vouch for anything.
            state.pop(jar_name, None)
            lockdir.write_text(reg_path, json.dumps(state))
            raise
        failed.pop(jar_name, None)
        if failed:
            state[_FAILED] = failed
        else:
            state.pop(_FAILED, None)
        state[jar_name] = fingerprint
        lockdir.write_text(reg_path, json.dumps(state))
        return True


# ---------------------------------------------------------------------------
# In-use guard: a jar must not be unloaded/reloaded while another test's app is
# running on it.
#
# The registry lock above serialises registrations against EACH OTHER, which is not the
# same thing: it does nothing to stop a reload landing in the middle of a sibling's
# deployed run. `LOAD OPEN PROCESSOR` replaces the loaded artifact cluster-wide, so a
# reload under a running app is exactly the "jar vanished / bad signature" class we set out
# to fix.
#
# So this is a reader-writer lock, which `filelock` does not provide (it is exclusive-only):
#   - every test holds it SHARED for as long as it is deployed and running, so any number of
#     tests run concurrently;
#   - a reload takes it EXCLUSIVE, so it waits for every running test to finish and blocks
#     new ones from starting until the new bytes are in place.
#
# The cost of the exclusive path is why the content diff matters so much: when the jar is
# byte-identical the reload is skipped entirely and nobody ever waits. The exclusive lock is
# reached only when the bytes genuinely changed.
# ---------------------------------------------------------------------------


# How long a reload may wait for running tests to drain before we give up and say so.
# Generous: it must outlast the slowest legitimate test, since the writer waits for every
# reader to finish. Bounded anyway, because a blocking flock with no timeout turns a lock
# bug into a silent hang -- the suite simply stops, with no clue which lock or which holder.
_LOCK_TIMEOUT_S = 900


class LockTimeout(RuntimeError):
    pass


@contextmanager
def _flock(path: Path, mode: int, timeout: float = _LOCK_TIMEOUT_S):
    """Take a flock on `path`, giving up after `timeout` seconds.

    NOT re-entrant, by construction: every call open()s the file afresh, and flock treats
    descriptors from separate open() calls as independent EVEN WITHIN ONE PROCESS. So taking
    the exclusive lock while already holding the shared one deadlocks against yourself. Any
    caller needing to upgrade must release first (see plugin.py's poison-recovery path).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = lockdir.open_append(path)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    kind = "exclusive" if mode == fcntl.LOCK_EX else "shared"
                    raise LockTimeout(
                        f"timed out after {timeout:.0f}s waiting for the {kind} lock on "
                        f"{path}. A reload waits for every running test to finish; if this "
                        f"fires, either a test is hung holding it or a caller tried to "
                        f"upgrade shared->exclusive without releasing first (self-deadlock)."
                    ) from None
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _use_lock(registry_dir=None, env=None) -> Path:
    """Machine-wide by default (stack.lock_path) so separate checkouts/worktrees pointed at
    one cluster actually serialise. `registry_dir` overrides it -- tests inject a tmp_path to
    get an isolated lock; production callers pass nothing."""
    if registry_dir is not None:
        return Path(registry_dir) / _scoped(_USE_LOCK, env)
    return stack.lock_path(_USE_LOCK, env).parent / _scoped(_USE_LOCK, env)


def in_use(registry_dir=None, env=None):
    """Shared lock held for the lifetime of a running test: many tests at once, but no
    reload while any of them is up."""
    return _flock(_use_lock(registry_dir, env), fcntl.LOCK_SH)


def exclusive_reload(registry_dir=None, env=None):
    """Exclusive lock for an unload+load: waits for every running test to release, and holds
    off new ones until the new bytes are loaded."""
    return _flock(_use_lock(registry_dir, env), fcntl.LOCK_EX)
