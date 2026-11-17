"""Infrastructure ownership for live runs (contract set 1.7.0, C7.1).

Every live execution declares how it owns the Striim cluster and the service containers it
uses, through ``SLT_INFRA_OWNERSHIP``:

* ``exclusive``: the run owns a freshly allocated stack. The framework allocates the stack
  prefix from the run id (an operator prefix is refused), takes the endpoint lease for the
  Striim host port, refuses leftover containers of that prefix, and refuses any Striim
  endpoint that is reachable or whose port is open before provisioning. It never adopts or
  redeploys an existing cluster. Destructive bring-up and session teardown stay allowed.
  Every process of the run (the lease holder and each joining xdist worker) registers as a
  member; the run's claim on the port lasts until its last member releases, and the endpoint
  check is decided once per run, so a joining worker binds to the run's own allocation.
* ``shared``: the run reuses a kept stack it does not own. ``SLT_KEEP_SERVICES=1`` is
  required, a per-process marker is registered for the endpoint, every service goes through
  one locked adapter (reuse a running container, else provision and keep it), and the cluster
  is never redeployed.

Undeclared is refused (``DEFAULT_INFRA_OWNERSHIP = None``). The refusal lives at the
live-execution entry points: ``livetest.plugin.pytest_collection_finish`` (live items were
collected) and the three console pre-flight functions. ``plugin._resolve_striim`` never
refuses by itself; a config without a declaration (a direct in-process caller such as a
hermetic test) keeps today's behaviour.

``livetest.plugin`` reaches this module through the ``lifecycle-hooks@1`` transform. This
module never imports the plugin.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from filelock import FileLock, Timeout

from livetest import dockercmd, lockdir, paths, stack

OWNERSHIP_ENV = "SLT_INFRA_OWNERSHIP"
MODES = ("exclusive", "shared")
# An undeclared live run is refused.
DEFAULT_INFRA_OWNERSHIP = None
SHARED_LOCK_TIMEOUT_S = 600.0
# SLT_SHARED_PROVISION=0 (a lane's .env): a shared run never starts a missing service, it names the start command.
SHARED_PROVISION_ENV = "SLT_SHARED_PROVISION"
# A shared marker's process start time is compared within this slack: a reused pid starts long after the
# marker was written, while two reads of one process's start time can differ by a clock adjustment.
_START_SLACK_S = 2.0
# An xdist worker of the run that holds the exclusive lease joins it; the holder writes the
# owner record right after acquiring, so a worker waits this long for it before refusing.
_OWNER_WAIT_S = 5.0
# 4.1 fix r2: the per-port registry lock serializes every lease acquire/probe and shared-marker write. It is
# held only for short non-waiting sections, never while waiting on another holder, so it cannot deadlock.
_REGISTRY_WAIT_S = 60.0
# r1 F4: how many times a declaration re-decides after the lease it was joining was released meanwhile.
_DECLARE_ATTEMPTS = 20
_STOP_HINT = "SLT_STACK_PREFIX={prefix} python -m livetest.cli stop all"


class InfraOwnershipError(Exception):
    """A refused or contradictory ownership declaration, or a bounded coordination failure."""


def _lock_dir() -> Path:
    return lockdir.ensure_dir(stack._LOCK_DIR)


def striim_endpoint(env) -> tuple[str, str, int]:
    """(url, host, port) of the Striim endpoint, resolved exactly as ``plugin._resolve_striim``
    resolves it."""
    url = (env.get("STRIIM_URL") or "").strip()
    if not url:
        svc_host = (env.get("SLT_SERVICES_HOST") or "").strip() or "localhost"
        url = f"http://{svc_host}:9080"
    else:
        svc_host = (env.get("SLT_SERVICES_HOST") or "").strip()
        if svc_host and "localhost" in url:
            url = url.replace("localhost", svc_host)
    parsed = urlparse(url if "://" in url else "http://" + url)
    return url, parsed.hostname or "localhost", parsed.port or 9080


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start(pid: int):
    """The process's start time (seconds since the epoch), or None when it cannot be read."""
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:                   # noqa: BLE001 - no such process, access denied, no psutil
        return None


def _docker_ps(argv):
    return dockercmd.run(argv)


@dataclass
class Infra:
    ownership: str
    port: int
    lock_dir: Path
    run_id: str = ""
    stack_prefix: str = ""
    lease_kind: str = ""            # held | joined | shared-marker
    lease_path: Path | None = None
    services: list = field(default_factory=list)
    striim: dict = field(default_factory=dict)
    teardown: dict | None = None    # session teardown outcome (C7.4): None, or {status: failed, failures}
    endpoint_proven: bool = False   # the run's endpoint check passed (this process or a same-run member)
    _lease: object = None
    _owner_path: Path | None = None
    _marker: Path | None = None
    _member: Path | None = None

    def record(self) -> dict:
        """C7.1 evidence: ``resources.infrastructure``."""
        return {
            "ownership": self.ownership,
            "stackPrefix": self.stack_prefix,
            "lockDir": str(self.lock_dir),
            "lease": {"kind": self.lease_kind, "path": str(self.lease_path) if self.lease_path else None},
            "services": list(self.services),
            "striim": dict(self.striim),
            "teardown": self.teardown,
        }

    def _drop_member(self) -> None:
        if self._member is not None:
            try:
                self._member.unlink()
            except FileNotFoundError:
                pass
            self._member = None

    def release(self) -> None:
        if self._marker is not None:
            try:
                self._marker.unlink()
            except FileNotFoundError:
                pass
            self._marker = None
        if self.ownership != "exclusive" or self.lease_path is None:
            self._drop_member()
            return
        # The run's claim (owner record, endpoint verdict) outlives this process while another member
        # of the same run is alive; the last member removes it.
        try:                            # 4.1 fix r2: the release probe is serialized with every other probe
            registry = _registry(self.lock_dir, self.port)
        except InfraOwnershipError as e:
            self._defer_release(e)      # r2 R2-2: nothing is decided or removed without the lock
            return
        try:
            self._drop_member()         # r1 F4: under the registry lock, so a joiner's revalidation sees it
            last = not _live_members(self.lease_path, self.run_id, _pid_alive)
            lease, own = self._lease, self._lease is not None
            if lease is None and last:
                lease = FileLock(str(self.lease_path), mode=lockdir.file_mode())
                try:
                    lease.acquire(timeout=0)
                    own = True
                except Timeout:
                    own = False
            if own:
                if last:
                    for extra in (Path(str(self.lease_path) + ".owner"), _verdict_path(self.lease_path)):
                        try:
                            extra.unlink()
                        except FileNotFoundError:
                            pass
                        except PermissionError:     # another user's record in the shared sticky dir:
                            lockdir.write_text(extra, "{}")     # empty it; no runId reads as stale
                lease.release()
            self._lease = None
        finally:
            registry.release()

    def _defer_release(self, error: InfraOwnershipError) -> None:
        """r2 R2-2: ``release`` could not take the registry lock, and a same-run joiner may be inside its locked
        revalidation. So nothing a declaration reads is touched: this process's member record, the owner record,
        the endpoint verdict and a held lease all stay, and this object keeps its references, so a later
        ``release()`` finishes the cleanup under the lock. If the process exits first, what remains is what a
        crashed member leaves: the OS drops the lease, a dead member pid is not live, and the next holder's
        stale-owner handling replaces the records. Reported on stderr, not through ``warnings`` (a filter could
        turn it into an error); never raises."""
        try:
            sys.stderr.write(
                f"WARNING: infrastructure release deferred for exclusive run {self.run_id} on port {self.port}: "
                f"{error}; member record {self._member}, owner record {self.lease_path}.owner and lease kept "
                f"until a later release or this process's exit\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


def _exclusive_lease_path(lock_dir: Path, port: int) -> Path:
    return lock_dir / f".slt-endpoint-{port}.exclusive"


def _registry_path(lock_dir: Path, port: int) -> Path:
    return lock_dir / f".slt-endpoint-{port}.registry.lock"


def _registry(lock_dir: Path, port: int, timeout: float = None) -> FileLock:
    """Acquire the port's registry lock (bounded) and return it; the caller releases it.

    Every check-then-mark on the endpoint runs under it: the exclusive side's lease acquire, its
    shared-marker and member checks and its owner write; the shared side's lease probe, member check and
    marker write; and ``release``'s lease probe. So a probe never observes another probe, and a shared
    and an exclusive declaration can never both pass their checks."""
    lock = FileLock(str(_registry_path(lock_dir, port)), mode=lockdir.file_mode())
    try:
        lock.acquire(timeout=_REGISTRY_WAIT_S if timeout is None else timeout)
    except Timeout:
        raise InfraOwnershipError(
            f"lock-timeout: {_registry_path(lock_dir, port)} not acquired within "
            f"{_REGISTRY_WAIT_S if timeout is None else timeout:.0f}s") from None
    return lock


def _shared_dir(lock_dir: Path, port: int) -> Path:
    return lock_dir / f".slt-endpoint-{port}.shared"


def _marker_live(path: Path, pid: int, pid_alive) -> bool:
    """A shared marker names a live session only while its pid is alive AND is still the process that wrote it.

    A marker records its process's start time (``started``); a pid whose process started at another time is a
    reused pid. A marker without one (written before markers recorded it, or not a marker record at all) is
    judged by its file time instead: the process that wrote it was already running then, so a process that
    started after the marker was written (a reused pid, or any process after a reboot) is not its writer.
    When the start time cannot be read, the pid alone decides."""
    if not pid_alive(pid):
        return False
    began = _process_start(pid)
    if began is None:
        return True
    record = _read_owner(path)
    started = record.get("started") if isinstance(record, dict) else None
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        return abs(began - started) <= _START_SLACK_S
    try:
        written = path.stat().st_mtime
    except OSError:
        return False
    return began <= written + _START_SLACK_S


def _live_shared_markers(lock_dir: Path, port: int, pid_alive) -> list[int]:
    """Live shared sessions on the port. Callers hold the port's registry lock: a marker that names no live
    session (a dead or reused pid) is removed here, so a marker never outlives its process for long and a
    later process given the same pid cannot pass for the session that wrote it."""
    return _live_markers(_shared_dir(lock_dir, port), pid_alive)


def _stopping_dir(lock_dir: Path, port: int) -> Path:
    return lock_dir / f".slt-endpoint-{port}.stopping"


def _live_stoppers(lock_dir: Path, port: int, pid_alive) -> list[int]:
    """Other live processes stopping or restarting the stack on the port (``reserve_stop``). Callers hold the
    port's registry lock; the records of dead and reused pids are removed as shared markers are."""
    return [pid for pid in _live_markers(_stopping_dir(lock_dir, port), pid_alive) if pid != os.getpid()]


def _refuse_while_stopping(lock_dir: Path, port: int, pid_alive, who: str) -> None:
    """A declaration is never admitted while another process stops or restarts the stack: it would start
    using what that stop is removing. Called under the registry lock, as one step with the marker write."""
    stopping = _live_stoppers(lock_dir, port, pid_alive)
    if stopping:
        raise InfraOwnershipError(
            f"{who} refused: a stop or restart of the stack on Striim port {port} is in progress (pids "
            f"{stopping}, {_stopping_dir(lock_dir, port)}); start again once it has finished")


def _live_markers(d: Path, pid_alive) -> list[int]:
    if not d.is_dir():
        return []
    alive = []
    for p in d.iterdir():
        try:
            pid = int(p.name)
        except ValueError:
            continue
        if _marker_live(p, pid, pid_alive):
            alive.append(pid)
            continue
        try:
            p.unlink()
        except OSError:                 # another user's marker in the shared sticky dir: still not live
            pass
    return sorted(alive)


def _members_dir(lease_path: Path) -> Path:
    return Path(str(lease_path) + ".members")


def _verdict_path(lease_path: Path) -> Path:
    return Path(str(lease_path) + ".endpoint")


def _live_members(lease_path: Path, run_id, pid_alive) -> list[int]:
    """Live pids registered as members of ``run_id`` for this lease, excluding this process."""
    d = _members_dir(lease_path)
    if not d.is_dir() or not run_id:
        return []
    alive = []
    for p in d.iterdir():
        try:
            pid = int(p.name)
        except ValueError:
            continue
        if pid == os.getpid() or (_read_owner(p) or {}).get("runId") != run_id:
            continue
        if pid_alive(pid):
            alive.append(pid)
    return sorted(alive)


def _add_member(lease_path: Path, run_id: str) -> Path:
    d = _members_dir(lease_path)
    lockdir.ensure_dir(d)
    member = d / str(os.getpid())
    lockdir.write_text(member, json.dumps({"runId": run_id, "pid": os.getpid()}))
    return member


def _exclusive_lease_held(lock_dir: Path, port: int) -> bool:
    """Probe the exclusive lease. Callers hold the port's registry lock (4.1 fix r2): every other short-lived
    acquisition of the lease (another probe, ``release``'s probe, an exclusive declaration's acquire) runs
    under the same lock, so a held lease here is a real exclusive holder."""
    lock = FileLock(str(_exclusive_lease_path(lock_dir, port)), mode=lockdir.file_mode())
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return True
    lock.release()
    return False


def _read_owner(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _stop_users(env, lock_dir: Path, port: int) -> str | None:
    """Why a stop of the stack on this endpoint must not run now, or None. Callers hold the port's registry lock.

    Read after the markers of dead and reused pids are pruned: a live shared session other than this process,
    or a live exclusive run other than this process's own run (its lease is held, or its owner or a member is
    alive). An exclusive run's leftover (no live holder or member) does not refuse."""
    shared = [pid for pid in _live_shared_markers(lock_dir, port, _pid_alive) if pid != os.getpid()]
    lease_path = _exclusive_lease_path(lock_dir, port)
    owner = _read_owner(Path(str(lease_path) + ".owner")) or {}
    run_id = owner.get("runId") or ""
    exclusive, held = [], False
    if not run_id or run_id != (env.get("SLT_RUN_EPOCH") or ""):
        exclusive = _live_members(lease_path, run_id, _pid_alive)
        pid = owner.get("pid")
        if isinstance(pid, int) and pid != os.getpid() and pid not in exclusive and _pid_alive(pid):
            exclusive.append(pid)
        held = _exclusive_lease_held(lock_dir, port)
    users = []
    if shared:
        users.append(f"shared session pids {shared}")
    if exclusive or held:
        users.append(f"exclusive run {run_id or 'unknown'} (pids {sorted(exclusive) or 'unknown'})")
    if not users:
        return None
    return f"other live sessions use the stack on Striim port {port}: {'; '.join(users)}"


# (lock dir, port) -> how many reservations this process holds there: the halves of one `livetest.cli stop`
# reserve again inside the command's own reservation, and only the outermost release removes the record.
_stops_held: dict = {}


class StopReservation:
    """This process is stopping or restarting the stack on a port. While its record exists and this process is
    alive, no declaration on the port is admitted (they refuse, naming this pid). ``release`` it in a
    ``finally`` once the destructive operation is over; a process that dies holding it leaves a record of a
    dead pid, which the next check removes. ``path`` None: a forced stop that could not reserve."""

    def __init__(self, lock_dir: Path, port: int, path: Path | None):
        self.lock_dir, self.port, self.path = lock_dir, port, path

    def release(self) -> None:
        if self.path is None:
            return
        key = (str(self.lock_dir), self.port)
        depth = _stops_held.get(key, 1) - 1
        if depth > 0:
            _stops_held[key] = depth
        else:
            _stops_held.pop(key, None)
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.path = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _reserve(lock_dir: Path, port: int) -> StopReservation:
    """Record this process as stopping the stack on ``port``. Callers hold the port's registry lock, and have
    just found nobody else using the stack in the same hold."""
    key = (str(lock_dir), port)
    d = _stopping_dir(lock_dir, port)
    path = d / str(os.getpid())
    if key not in _stops_held:
        lockdir.ensure_dir(d)
        lockdir.write_text(path, json.dumps({"pid": os.getpid(), "started": _process_start(os.getpid())}))
    _stops_held[key] = _stops_held.get(key, 0) + 1
    return StopReservation(lock_dir, port, path)


def reserve_stop(env, force: bool = False):
    """Reserve a stop of the stack on this endpoint: ``(reservation, None)``, or ``(None, why)`` when refused.

    The check that no other live session uses the stack (``_stop_users``) and the record that this process is
    stopping it are one step under the port's registry lock, and every declaration reads that record under the
    same lock, so no session can be admitted between the check and the `down -v` that follows: until the
    reservation is released, a declaration on the port refuses. A registry lock that cannot be taken refuses:
    nothing was checked. ``force`` skips the check but still reserves, so no new session joins a stack being
    removed; a forced stop whose reservation cannot be made goes ahead without one."""
    _url, _host, port = striim_endpoint(env)
    lock_dir = _lock_dir()
    try:
        registry = _registry(lock_dir, port)
    except InfraOwnershipError as e:
        if force:
            return StopReservation(lock_dir, port, None), None
        return None, f"the ownership registry for Striim port {port} could not be read ({e})"
    try:
        why = None if force else _stop_users(env, lock_dir, port)
        if why is not None:
            return None, why
        try:
            return _reserve(lock_dir, port), None
        except OSError as e:
            if force:
                return StopReservation(lock_dir, port, None), None
            return None, f"the stop of the stack on Striim port {port} could not be recorded ({e})"
    finally:
        registry.release()


def reserve_restart(infra: "Infra"):
    """``reserve_stop`` for the app-node restart of a shared declaration: ``(reservation, [])``, or
    ``(None, pids)`` when other live shared sessions use the cluster. Any other declaration needs none."""
    if not isinstance(infra, Infra) or infra.ownership != "shared":
        return StopReservation(Path(), 0, None), []
    registry = _registry(infra.lock_dir, infra.port)
    try:
        others = [pid for pid in _live_shared_markers(infra.lock_dir, infra.port, _pid_alive) if pid != os.getpid()]
        if others:
            return None, others
        return _reserve(infra.lock_dir, infra.port), []
    finally:
        registry.release()


def declare(env=None, *, run=None, pid_alive=None) -> Infra:
    """Declare this process's infrastructure ownership, or raise ``InfraOwnershipError``.

    ``env`` is updated in place for exclusive mode (``SLT_RUN_EPOCH`` when unset, and the
    allocated ``SLT_STACK_PREFIX``), before any compose or docker call reads it."""
    env = os.environ if env is None else env
    run = run or _docker_ps
    pid_alive = pid_alive or _pid_alive
    mode = (env.get(OWNERSHIP_ENV) or "").strip() or DEFAULT_INFRA_OWNERSHIP
    if mode is None:
        raise InfraOwnershipError(
            f"live runs must declare infrastructure ownership: set {OWNERSHIP_ENV}=exclusive "
            f"(a freshly allocated stack this run owns) or {OWNERSHIP_ENV}=shared (a kept stack, "
            f"with SLT_KEEP_SERVICES=1)")
    if mode not in MODES:
        raise InfraOwnershipError(
            f"{OWNERSHIP_ENV}={mode!r} is not an accepted value: use exclusive or shared")
    _url, _host, port = striim_endpoint(env)
    lock_dir = _lock_dir()
    if mode == "shared":
        return _declare_shared(env, port, lock_dir)
    return _declare_exclusive(env, port, lock_dir, run, pid_alive)


def _declare_shared(env, port: int, lock_dir: Path) -> Infra:
    if not paths.flag("SLT_KEEP_SERVICES", env):
        raise InfraOwnershipError(
            f"{OWNERSHIP_ENV}=shared requires SLT_KEEP_SERVICES=1: without it service bring-up "
            f"wipes volumes and session end tears the shared stack down")
    registry = _registry(lock_dir, port)     # 4.1 fix r2: probe, member check and marker write as one step
    try:
        owner = _read_owner(Path(str(_exclusive_lease_path(lock_dir, port)) + ".owner")) or {}
        if _exclusive_lease_held(lock_dir, port) or _live_members(_exclusive_lease_path(lock_dir, port),
                                                                 owner.get("runId"), _pid_alive):
            raise InfraOwnershipError(
                f"{OWNERSHIP_ENV}=shared refused: an exclusive run holds the endpoint lease for port "
                f"{port} ({_exclusive_lease_path(lock_dir, port)}; run {owner.get('runId', 'unknown')})")
        _refuse_while_stopping(lock_dir, port, _pid_alive, f"{OWNERSHIP_ENV}=shared")
        _live_shared_markers(lock_dir, port, _pid_alive)    # prunes markers of dead and reused pids
        d = _shared_dir(lock_dir, port)
        lockdir.ensure_dir(d)
        marker = d / str(os.getpid())
        lockdir.write_text(marker, json.dumps({"pid": os.getpid(), "runId": env.get("SLT_RUN_EPOCH", ""),
                                               "started": _process_start(os.getpid())}))
    finally:
        registry.release()
    return Infra("shared", port, lock_dir, run_id=env.get("SLT_RUN_EPOCH", ""),
                 stack_prefix=stack.prefix(env), lease_kind="shared-marker", lease_path=marker,
                 _marker=marker)


def _declare_exclusive(env, port: int, lock_dir: Path, run, pid_alive) -> Infra:
    run_id = env.get("SLT_RUN_EPOCH") or ""
    if not run_id:
        run_id = env["SLT_RUN_EPOCH"] = uuid.uuid4().hex[:12]
    allocated = "x" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
    operator = env.get("SLT_STACK_PREFIX") or ""
    if operator and operator != allocated:
        raise InfraOwnershipError(
            f"{OWNERSHIP_ENV}=exclusive refuses an operator SLT_STACK_PREFIX ({operator!r}): the "
            f"framework allocates the exclusive stack prefix from the run id; unset SLT_STACK_PREFIX")
    lease_path = _exclusive_lease_path(lock_dir, port)
    owner_path = Path(str(lease_path) + ".owner")
    # r1 F4: each attempt either holds the lease or joins it; a joiner that finds the lease released meanwhile
    # decides again from the top, so every outcome is checked against the endpoint's current registry.
    leftover = None             # the allocated prefix's containers, read outside the registry lock
    for _attempt in range(_DECLARE_ATTEMPTS):
        held = _hold_exclusive(env, port, lock_dir, run, pid_alive, run_id, allocated, lease_path, owner_path,
                               leftover)
        if held is _SCAN:
            leftover = _leftover_containers(run, allocated)
            continue
        if held is not None:
            _report_orphaned_stacks(run, lock_dir, allocated, pid_alive)
            return held
        joined = _join_exclusive(env, port, lock_dir, run_id, allocated, lease_path, owner_path, pid_alive)
        if joined is not None:
            return joined
    raise InfraOwnershipError(
        f"lock-timeout: the exclusive endpoint lease for port {port} ({lease_path}) was released and taken "
        f"{_DECLARE_ATTEMPTS} times while this process declared")


_SCAN = object()
_EXCLUSIVE_NAME = re.compile(r"^(x[0-9a-f]{8})-slt-")


def _leftover_containers(run, allocated: str) -> str:
    out = run(["docker", "ps", "-a", "--filter", f"name=^{allocated}-slt-", "-q"])
    return (getattr(out, "stdout", "") or "").strip()


def _live_exclusive_prefixes(lock_dir: Path, pid_alive) -> set:
    """The stack prefixes of exclusive runs that still have a live holder or member, on any port."""
    live = set()
    for owner_path in lock_dir.glob(".slt-endpoint-*.exclusive.owner"):
        owner = _read_owner(owner_path) or {}
        run_id = owner.get("runId")
        if not run_id:
            continue
        lease_path = Path(str(owner_path)[:-len(".owner")])
        pid = owner.get("pid")
        if (isinstance(pid, int) and pid_alive(pid)) or _live_members(lease_path, run_id, pid_alive):
            live.add("x" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8])
    return live


def _report_orphaned_stacks(run, lock_dir: Path, allocated: str, pid_alive) -> None:
    """Name the containers of earlier exclusive runs that no live process owns. Each run allocates a new
    prefix, so nothing else would look for them again, and they keep their host ports. Reported on stderr,
    never removed; a failed listing reports nothing."""
    try:
        out = run(["docker", "ps", "-a", "--filter", "name=^x", "--format", "{{.Names}}"])
    except Exception:                   # noqa: BLE001 - a report, never a reason to refuse
        return
    if getattr(out, "returncode", 0):
        return
    stacks: dict = {}
    for name in (getattr(out, "stdout", "") or "").split():
        m = _EXCLUSIVE_NAME.match(name)
        if m and m.group(1) != allocated:
            stacks.setdefault(m.group(1), []).append(name)
    if not stacks:
        return
    live = _live_exclusive_prefixes(lock_dir, pid_alive)
    for prefix, names in sorted(stacks.items()):
        if prefix in live:
            continue
        try:
            sys.stderr.write(f"WARNING: containers of an earlier exclusive run are still on this machine and keep "
                             f"their host ports: {', '.join(sorted(names))}; stop them with: "
                             f"{_STOP_HINT.format(prefix=prefix)}\n")
        except (OSError, ValueError):
            pass


def _hold_exclusive(env, port, lock_dir, run, pid_alive, run_id, allocated, lease_path, owner_path,
                    leftover=None):
    """Acquire the free lease and run every check up to the owner/member write under the registry lock
    (4.1 fix r2). ``None``: another process holds the lease (the registry lock is released again).
    ``_SCAN``: the lease was free but the allocated prefix's containers have not been listed yet; the lease is
    released and the caller lists them outside the registry lock (``leftover``), then decides again. A
    `docker ps` inside the lock made a hung Docker daemon time out every other declaration on the port."""
    lease = FileLock(str(lease_path), mode=lockdir.file_mode())
    registry = _registry(lock_dir, port)
    try:
        lease.acquire(timeout=0)
    except Timeout:
        registry.release()
        return None
    try:
        owner = _read_owner(owner_path) or {}
        # The lease file is free, but a run whose holder finished early may still have live members.
        still = _live_members(lease_path, owner.get("runId"), pid_alive)
        if still and owner.get("runId") != run_id:
            raise InfraOwnershipError(
                f"{OWNERSHIP_ENV}=exclusive refused: exclusive run {owner.get('runId')} is still active on "
                f"port {port} (member pids {still}, {lease_path})")
        alive = _live_shared_markers(lock_dir, port, pid_alive)
        if alive:
            raise InfraOwnershipError(
                f"{OWNERSHIP_ENV}=exclusive refused: shared run(s) are registered on port {port} "
                f"(pids {alive}, {_shared_dir(lock_dir, port)})")
        _refuse_while_stopping(lock_dir, port, pid_alive, f"{OWNERSHIP_ENV}=exclusive")
        if not still:   # a same-run takeover keeps the run's own stack; anything else must find none
            if leftover is None:
                lease.release()
                return _SCAN
            if leftover:
                raise InfraOwnershipError(
                    f"{OWNERSHIP_ENV}=exclusive refused: containers of the allocated prefix "
                    f"{allocated!r} already exist (a leftover stack); stop them first: "
                    + _STOP_HINT.format(prefix=allocated))
        lockdir.write_text(owner_path, json.dumps({"runId": run_id, "pid": os.getpid()}))
        member = _add_member(lease_path, run_id)
    except BaseException:
        lease.release()
        raise
    finally:
        registry.release()
    env["SLT_STACK_PREFIX"] = allocated
    return Infra("exclusive", port, lock_dir, run_id=run_id, stack_prefix=allocated,
                 lease_kind="held", lease_path=lease_path, _lease=lease, _owner_path=owner_path,
                 _member=member)


def _join_exclusive(env, port, lock_dir, run_id, allocated, lease_path, owner_path, pid_alive) -> Infra | None:
    """Another process holds the lease. Only a process of the SAME run (an xdist worker, which declares in its
    own pytest_collection_finish) may join it; anything else is refused.

    r1 F4: the owner record is awaited without the registry lock. The join is then decided under it, as one
    step with ``release`` and every other declaration: the lease must still be held, the owner record must
    still name this run and no shared marker may be live; only then is the member registered. ``None``: the
    lease was released meanwhile, and the caller decides again from the top."""
    deadline = time.monotonic() + _OWNER_WAIT_S
    while True:
        owner = _read_owner(owner_path)
        while owner is None and time.monotonic() < deadline:
            time.sleep(0.05)
            owner = _read_owner(owner_path)
        registry = _registry(lock_dir, port)
        try:
            if not _exclusive_lease_held(lock_dir, port):
                return None
            owner = _read_owner(owner_path)
            if owner is not None or time.monotonic() >= deadline:
                if not owner or owner.get("runId") != run_id:
                    raise InfraOwnershipError(
                        f"{OWNERSHIP_ENV}=exclusive refused: another exclusive run holds the endpoint lease for "
                        f"port {port} ({lease_path}; run {(owner or {}).get('runId', 'unknown')})")
                alive = _live_shared_markers(lock_dir, port, pid_alive)
                if alive:
                    raise InfraOwnershipError(
                        f"{OWNERSHIP_ENV}=exclusive refused: shared run(s) are registered on port {port} "
                        f"(pids {alive}, {_shared_dir(lock_dir, port)})")
                _refuse_while_stopping(lock_dir, port, pid_alive, f"{OWNERSHIP_ENV}=exclusive")
                member = _add_member(lease_path, run_id)
                break
        finally:
            registry.release()
    env["SLT_STACK_PREFIX"] = allocated
    return Infra("exclusive", port, lock_dir, run_id=run_id, stack_prefix=allocated,
                 lease_kind="joined", lease_path=lease_path, _member=member)


def declare_if_live(session, live_item_type) -> Infra | None:
    """``pytest_collection_finish``: declare when at least one live item was collected and the
    session is not collect-only. A refusal is a ``pytest.UsageError`` (pytest exit 4, C5 exit 2)."""
    import pytest
    config = session.config
    if getattr(config, "_slt_infra", None) is not None:
        return config._slt_infra
    if getattr(getattr(config, "option", None), "collectonly", False):
        return None
    if not any(isinstance(it, live_item_type) for it in getattr(session, "items", ())):
        return None
    try:
        config._slt_infra = declare(os.environ)
    except InfraOwnershipError as e:
        if hasattr(config, "workerinput"):
            # An xdist worker never refuses at collection (xdist reports that as a crashed node). The
            # controller refuses from the executable selection the workers report (declare_for_distributed_selection),
            # before scheduling; a live item that would still execute in this worker refuses when it runs.
            _refuse_items(session, live_item_type, str(e))
            return None
        raise pytest.UsageError(str(e)) from None
    return config._slt_infra


def _refused_runtest(message: str) -> None:
    raise InfraOwnershipError(message)


def _refuse_items(session, live_item_type, message: str) -> None:
    import functools
    for it in getattr(session, "items", ()):
        if isinstance(it, live_item_type):
            it.runtest = functools.partial(_refused_runtest, message)


def declare_if_distributed_live(config) -> Infra | None:
    """``pytest_configure`` of an xdist controller.

    The controller collects nothing; declaring from the invocation arguments refused runs whose live cases were
    all deselected. It now only marks a distributed, non-collect-only controller; the declaration is made from
    the executable selection in ``declare_for_distributed_selection`` (``pytest_xdist_node_collection_finished``),
    before any test is scheduled."""
    if hasattr(config, "workerinput") or getattr(config, "_slt_infra", None) is not None:
        return getattr(config, "_slt_infra", None)
    option = getattr(config, "option", None)
    workers, dist = getattr(option, "numprocesses", None), getattr(option, "dist", "no")
    if not getattr(option, "collectonly", False) and (workers or (dist and dist != "no")):
        config._slt_distributed = True
    return None


def declare_for_distributed_selection(config, ids) -> Infra | None:
    """``pytest_xdist_node_collection_finished`` on the controller: a node reported its executable
    selection (after deselection). When it holds a live case, the controller declares ownership before xdist
    schedules anything; a refusal is a ``pytest.UsageError`` (rc 4) with the ownership message. An empty or
    non-live selection needs no declaration (serial and distributed runs then agree: exit 5)."""
    import pytest
    if config is None or hasattr(config, "workerinput") or getattr(config, "_slt_infra", None) is not None:
        return getattr(config, "_slt_infra", None) if config is not None else None
    if getattr(getattr(config, "option", None), "collectonly", False):
        return None
    if not any("test.yaml::" in str(nid) for nid in (ids or ())):
        return None
    try:
        config._slt_infra = declare(os.environ)
    except InfraOwnershipError as e:
        raise pytest.UsageError(str(e)) from None
    return config._slt_infra


def declare_or_log(env, log) -> Infra | None:
    """Console pre-flight entry points: declare eagerly; on refusal log and return None (the
    caller returns exit 2)."""
    try:
        return declare(env)
    except InfraOwnershipError as e:
        log(f"ERROR: {e}")
        return None


def release(config) -> None:
    infra = getattr(config, "_slt_infra", None)
    if infra is not None:
        infra.release()


def of(config) -> Infra | None:
    """The declaration on ``config``, or None for a direct in-process caller (legacy path)."""
    return getattr(config, "_slt_infra", None)


def forbid_redeploy(config, reason: str) -> bool:
    """Called from both redeploy branches of ``_resolve_striim`` when a reason exists. True means
    the resolver must return None without ``cluster_down``: shared never redeploys; exclusive
    never adopts an existing cluster (E1; H0d normally refuses before this point)."""
    infra = of(config)
    if infra is None:
        return False
    if infra.ownership == "shared":
        why = f"shared cluster identity mismatch: {reason}; shared mode never redeploys"
    else:
        why = (f"exclusive cluster identity mismatch: {reason}; exclusive mode never adopts or "
               f"redeploys an existing cluster")
    infra.striim = {"status": "refused-identity-mismatch", "reason": reason}
    config._slt_striim = None
    config._slt_striim_reason = why
    return True


def _endpoint_refusal(url, host, port, reachable, port_open) -> str | None:
    if reachable():
        return (f"exclusive ownership: Striim endpoint {url} is already reachable before "
                f"provisioning; exclusive never adopts an existing cluster")
    if port_open(host, port):
        return (f"exclusive ownership: port {host}:{port} is already open before provisioning; "
                f"exclusive never adopts an existing endpoint")
    return None


def before_cluster_resolution(config, url, host, port, reachable, port_open) -> str | None:
    """Exclusive: a reachable endpoint, or an open port, before provisioning is somebody else's
    (or a stale) cluster; return the refusal reason. Shared and undeclared: None.

    The check is decided once per run under the lease: the first member records the verdict and
    every later member of the same run (a delayed xdist worker, when the run's own cluster is
    already up) uses it instead of probing an endpoint its run has since provisioned."""
    infra = of(config)
    if infra is None or infra.ownership != "exclusive":
        return None
    if infra.lease_path is None or not infra.run_id:
        why = _endpoint_refusal(url, host, port, reachable, port_open)
    else:
        verdict = _verdict_path(infra.lease_path)
        lock = FileLock(str(verdict) + ".lock", mode=lockdir.file_mode())
        try:
            lock.acquire(timeout=SHARED_LOCK_TIMEOUT_S)
        except Timeout:
            why = f"lock-timeout: {verdict}.lock not acquired within {SHARED_LOCK_TIMEOUT_S:.0f}s"
        else:
            try:
                rec = _read_owner(verdict)
                if rec and rec.get("runId") == infra.run_id and rec.get("url") == url:
                    why = rec.get("refused")
                else:
                    why = _endpoint_refusal(url, host, port, reachable, port_open)
                    lockdir.write_text(verdict, json.dumps({"runId": infra.run_id, "url": url, "refused": why,
                                                   "pid": os.getpid()}))
            finally:
                lock.release()
    if why:
        infra.striim = {"status": "refused-endpoint-in-use", "url": url}
        return why
    infra.endpoint_proven = True
    return None


def _published_host_ports(container: str, run) -> set[str]:
    out = run(["docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", container])
    try:
        ports = json.loads((getattr(out, "stdout", "") or "").strip() or "null") or {}
    except ValueError:
        return set()
    return {str(b.get("HostPort")) for binds in ports.values() if binds for b in binds}


def bind_striim(config, ctx, provisioned: bool, container: str, run=None) -> str | None:
    """Record the cluster's status; exclusive also requires the allocated ``slt-striim``
    container to publish the endpoint's port. Returns a refusal reason or None."""
    infra = of(config)
    if infra is None:
        return None
    _url, _host, port = striim_endpoint({"STRIIM_URL": ctx.url})
    if infra.ownership == "shared":
        if getattr(ctx, "mode", None) == "native":      # a native Striim: no container of the stack
            infra.striim = {"status": "external", "container": None, "urlPort": port, "boundToAllocated": False}
            return None
        infra.striim = {"status": "provisioned-and-kept" if provisioned else "reused",
                        "container": container, "urlPort": port, "boundToAllocated": False}
        return None
    run = run or _docker_ps
    # A same-run member that did not provision itself binds only after its run proved the endpoint
    # free before provisioning, and only to the allocated container publishing the port.
    bound = (provisioned or infra.endpoint_proven) and str(port) in _published_host_ports(container, run)
    infra.striim = {"status": "owned" if bound else "refused-unbound", "container": container,
                    "urlPort": port, "boundToAllocated": bound,
                    "provisionedBy": "this-process" if provisioned else "same-run"}
    if bound:
        return None
    return (f"exclusive ownership: the Striim endpoint port {port} is not published by the "
            f"allocated container {container} (provisioned={provisioned}); refusing to test "
            f"against a cluster this run does not own")


def teardown_failed(config, what: str, exc: BaseException) -> None:
    """Session teardown of owned infrastructure failed: recorded for the final run outcome (C7.4)."""
    failures = config.__dict__.setdefault("_slt_teardown_failures", [])
    failures.append({"resource": what, "error": f"{type(exc).__name__}: {exc}"})
    infra = of(config)
    if infra is not None:
        infra.teardown = {"status": "failed", "failures": list(failures)}


def shared_provision_lock(container: str) -> str:
    """The shared provisioning lock of one service container, in the machine-wide lock dir. One lock per
    container, so a Postgres bring-up never waits behind a cold Oracle start."""
    return f".slt-shared-provision-{container}.lock"


def _shared_provision_allowed(env) -> bool:
    """``SLT_SHARED_PROVISION``: unset or 1 starts a missing shared service (the default); 0 refuses.
    A switch like the others (livetest.paths.flag): any other value is refused."""
    from livetest import paths
    try:
        return paths.flag(SHARED_PROVISION_ENV, env)
    except paths.FlagError as e:
        raise InfraOwnershipError(str(e)) from None


def resolve_service(infra, name, env, started, resolve_fn, progress=None, *, load=None,
                    container_running=None, lock_timeout=SHARED_LOCK_TIMEOUT_S, acquire_lock=None,
                    forget=None):
    """The one service adapter for serial and parallel callers (C7.1, N3).

    Undeclared and exclusive: ``resolve_fn`` unchanged. Shared: under the container's machine-wide
    shared provisioning lock (bounded), recheck the prefixed container inside the lock; a running one is
    pre-registered in ``started`` (``resolve`` then sees ``first == False``) and recorded
    ``reused``; otherwise its provision records are forgotten (a record outliving its container made the
    bring-up a no-op) and ``resolve_fn`` provisions it, recorded ``provisioned-and-kept``. Under
    ``SLT_SHARED_PROVISION=0`` a missing container is refused instead, naming the start command.
    ``ensure_provisioned_once`` (parallel branch) uses a different lock file, so nothing is
    acquired recursively. ``acquire_lock(lock)``, when given, replaces the bounded acquire."""
    if infra is None:
        return resolve_fn(name, env, started, progress=progress)
    if load is None:
        from livetest.registry import load_service as load
    if container_running is None:
        from livetest.services import container_running
    defn = load(name)
    container = defn.container
    if infra.ownership == "exclusive":
        resolved = resolve_fn(name, env, started, progress=progress)
        infra.services.append({"name": name, "container": container, "status": "owned"})
        return resolved
    if getattr(defn, "live_override_env", None) and env.get(defn.live_override_env):
        resolved = resolve_fn(name, env, started, progress=progress)
        infra.services.append({"name": name, "container": container, "status": "external"})
        return resolved
    if container in started:
        return resolve_fn(name, env, started, progress=progress)
    allowed = _shared_provision_allowed(env)
    lock_path = _lock_dir() / shared_provision_lock(container)
    lock = FileLock(str(lock_path), mode=lockdir.file_mode())
    try:
        if acquire_lock is not None:
            acquire_lock(lock)
        else:
            lock.acquire(timeout=lock_timeout)
    except Timeout:
        raise InfraOwnershipError(
            f"lock-timeout: {lock_path} not acquired within "
            f"{lock_timeout:.0f}s while resolving shared service {name}") from None
    try:
        if container_running(container):
            started.add(container)
            status = "reused"
        else:
            if not allowed:
                prefix = stack.prefix(env)
                raise InfraOwnershipError(
                    f"shared service {name} ({container}) is not running, and {SHARED_PROVISION_ENV}=0 forbids "
                    f"starting it here; start it with: "
                    f"{f'SLT_STACK_PREFIX={prefix} ' if prefix else ''}python -m livetest.cli start {name}")
            if forget is None:
                from livetest.services import forget_provisioned as forget
            try:
                # Only where `resolve` consults the provision registry (its parallel branch): a serial
                # run never reads it, and needs no state dir for it.
                from livetest import paths
                if env.get("PYTEST_XDIST_WORKER") or paths.flag("SLT_PARALLEL", env):
                    forget([container, f"{name}-setup"], timeout=dockercmd.LOCK_WAIT_S)
            except Timeout:
                raise InfraOwnershipError(
                    f"lock-timeout: the provision registry was not available within "
                    f"{dockercmd.LOCK_WAIT_S:.0f}s while resolving shared service {name}") from None
            status = "provisioned-and-kept"
        resolved = resolve_fn(name, env, started, progress=progress)
    finally:
        lock.release()
    infra.services.append({"name": name, "container": container, "status": status})
    return resolved


# ---------------------------------------------------------------------------------------------
# (C4 1.9.0): observed Striim and service identities for the evidence envelope, bounded.
# ---------------------------------------------------------------------------------------------
DOCKER_INSPECT_TIMEOUT_S = 10.0


def _docker_inspect(argv):
    import subprocess
    from types import SimpleNamespace
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=DOCKER_INSPECT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return SimpleNamespace(returncode=124, stdout="", stderr=f"timed out after {DOCKER_INSPECT_TIMEOUT_S:.0f}s")
    except OSError as e:
        return SimpleNamespace(returncode=127, stdout="", stderr=str(e))


def strip_userinfo(url):
    """``url`` without its credentials: up to the last ``@`` of the authority (``evidence._USERINFO``)."""
    from livetest.evidence import _USERINFO
    return _USERINFO.sub(r"\g<s>", url) if isinstance(url, str) else url


def inspect_image(container: str) -> dict:
    """``{imageId, image}`` of a running container, or ``{"unobserved": why}``; one bounded ``docker inspect``."""
    r = _docker_inspect(["docker", "inspect", "-f", "{{.Image}}|{{.Config.Image}}", container])
    out = (getattr(r, "stdout", "") or "").strip()
    if getattr(r, "returncode", 1) != 0 or out.count("|") != 1 or not all(out.split("|")):
        return {"unobserved": f"docker inspect {container}: rc {getattr(r, 'returncode', None)} "
                              f"{(getattr(r, 'stderr', '') or '').strip()[:200]}".strip()}
    image_id, image = out.split("|")
    return {"imageId": image_id, "image": image}


def platform_jars(names) -> list[str]:
    """The ``Platform-<ver>.jar`` names among ``names`` (``-sources``/``-javadoc`` skipped), sorted: the rule of
    ``releases.detect_release`` (a synced file, so it keeps its own copy; test_runtime_version pins the two equal)."""
    return sorted(n for n in names if n.startswith("Platform-") and n.endswith(".jar")
                  and not n.endswith(("-sources.jar", "-javadoc.jar")))


# The fixed probe the running container answers: its lib listing, a marker, then striim-node's dpkg version (empty
# when dpkg or the package is absent). The container name is an argv element, never part of this text.
RUNTIME_PROBE_MARK = "slt-dpkg"
RUNTIME_PROBE_SCRIPT = ("ls -1 /opt/striim/lib && echo " + RUNTIME_PROBE_MARK +
                        " && { dpkg-query -W -f='${Version}\\n' striim-node 2>/dev/null || true; }")


def runtime_version(config, ctx, container):
    """The running Striim's version, observed from the runtime -- never from the image reference.
    One bounded ``docker exec`` of ``RUNTIME_PROBE_SCRIPT``: exactly one ``Platform-<v>.jar`` (``platform_jars``)
    decides, and dpkg's ``striim-node`` version corroborates when present. ``{"unreadable": why}`` when the runtime
    answers ambiguously; ``{"unobserved": why}`` when it cannot be asked."""
    from livetest import evidence
    if container is None:
        return {"unobserved": "native endpoint: no runtime installation is observable from the harness"}
    r = _docker_inspect(["docker", "exec", container, "sh", "-c", RUNTIME_PROBE_SCRIPT])
    rc, out = getattr(r, "returncode", 1), getattr(r, "stdout", "") or ""
    lines = out.splitlines()
    if rc != 0 or RUNTIME_PROBE_MARK not in lines:
        return {"unobserved": f"docker exec {container} (runtime version probe): rc {rc} "
                              f"{(getattr(r, 'stderr', '') or '').strip()[:200]}".strip()}
    mark = lines.index(RUNTIME_PROBE_MARK)
    jars = platform_jars(line.strip() for line in lines[:mark])
    dpkg = "\n".join(lines[mark + 1:]).strip()
    if len(jars) != 1:
        return {"unreadable": f"{len(jars)} Platform-*.jar under /opt/striim/lib in {container}: {jars[:4]}"}
    version = jars[0][len("Platform-"):-len(".jar")]
    if not evidence._VERSION.match(version):
        return {"unreadable": f"malformed runtime version {version[:80]!r} from {jars[0][:120]!r}"}
    if dpkg and dpkg != version:
        return {"unreadable": f"{jars[0]} disagrees with dpkg striim-node {dpkg[:80]!r}"}
    return version


def observe_striim(config) -> dict:
    """``runtime.striim``: the expected release and the observed endpoint, container, image (metadata), image id
    and the runtime version (``runtime_version``; never the image tag). Cached per session."""
    cached = getattr(config, "_slt_striim_observed", None)
    if cached is not None:
        # tryouts is read fresh: it is set once the cluster is verified, which may follow the first call.
        return {**cached, "tryouts": observe_tryouts(config)}
    release = getattr(config, "_slt_release", None)
    expected = dict(release) if isinstance(release, dict) else {"unobserved": "the Striim release was not resolved"}
    ctx = getattr(config, "_slt_striim", None)
    if ctx is None:
        observed = {"unobserved": "no Striim endpoint was resolved in this session"}
    elif getattr(ctx, "mode", None) != "docker":
        observed = {"url": strip_userinfo(ctx.url), "container": None, "consoleReachable": True,
                    "version": runtime_version(config, ctx, None),
                    "image": {"reason": "native endpoint"}, "imageId": {"reason": "native endpoint"}}
    else:
        from livetest import stack
        container = stack.striim_container()
        img = inspect_image(container)
        image, image_id = (img, img) if "unobserved" in img else (img["image"], img["imageId"])
        observed = {"url": strip_userinfo(ctx.url), "version": runtime_version(config, ctx, container),
                    "container": container, "image": image, "imageId": image_id, "consoleReachable": True}
    result = {"expected": expected, "observed": observed}
    try:
        config._slt_striim_observed = result
    except Exception:                   # noqa: BLE001 - a frozen config just is not cached
        pass
    return {**result, "tryouts": observe_tryouts(config)}


def observe_tryouts(config):
    """``runtime.striim.tryouts``: the tryout jars the cluster was verified to run (livetest.tryout), [] for
    the stock image. Configured but not verified is ``{"unobserved": why}``, which never qualifies, so a
    tryout run can never read as a stock one."""
    verified = getattr(config, "_slt_tryouts", None)
    if isinstance(verified, list) and verified:
        return [dict(r) for r in verified]
    if isinstance(verified, dict):      # a stock run whose members could not all be inspected
        return dict(verified)
    from livetest import tryout
    try:
        configured = bool(tryout.configured_value())
    except Exception as e:              # noqa: BLE001
        return {"unreadable": f"{tryout.TRYOUT_ENV}: {type(e).__name__}: {e}"}
    if configured:
        return {"unobserved": f"{tryout.TRYOUT_ENV} is set but no running cluster was verified to run its jars"}
    return []


def observe_services(config):
    """``runtime.services``: the declared services with their observed image and image id (external
    services carry the reason). Without a declaration the services are unobserved."""
    infra = of(config) if config is not None else None
    if infra is None:
        return {"unobserved": "no infrastructure declaration (SLT_INFRA_OWNERSHIP) in this process"}
    out = []
    cache = config.__dict__.setdefault("_slt_service_images", {})
    for svc in infra.services:
        entry = dict(svc)
        if svc.get("status") == "external":
            entry.update(image={"reason": "external service"}, imageId={"reason": "external service"})
        else:
            container = svc.get("container") or ""
            if container not in cache:
                cache[container] = inspect_image(container)
            img = cache[container]
            entry.update(img if "unobserved" not in img else {"image": img, "imageId": img})
        out.append(entry)
    return out
