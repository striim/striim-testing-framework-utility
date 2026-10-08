"""The `service_outage` action: stop a required service's container while the app runs, start it
again (or have the service restart in place, signal RESTART), and watch the app ride it out.
Like recovery.py it does not judge data; the test's assertions do. `run` is injectable for the
docker calls, `client` for the status reads.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

from livetest import registry


class OutageError(Exception):
    pass


def _sh(argv, run=None, timeout=None):
    run = run or (lambda a: subprocess.run(a, capture_output=True, text=True, timeout=timeout))
    return run(argv)


def _say(report, msg):
    if report:
        try:
            report(msg)
        except Exception:
            pass


def container_for(service: str, env=None) -> tuple[str | None, str]:
    """(container, "") for a Docker-managed service, else (None, reason) and the test skips."""
    env = os.environ if env is None else env
    try:
        defn = registry.load_service(service, env=env)
    except registry.RegistryError as e:
        return None, f"service_outage: {e}"
    why = registry.unavailable(defn, env)
    if why:
        return None, f"service_outage: service {service} unavailable: {why}"
    if defn.live_override_env and (env.get(defn.live_override_env) or "").strip():
        return None, (f"service_outage needs the Docker {service} container; "
                      f"{defn.live_override_env} points at an existing instance")
    if not defn.container:
        return None, f"service_outage: service {service} has no container"
    return defn.container, ""


#: Wait for the container to exit after a service's `graceful_stop`, when it sets no timeout.
GRACEFUL_STOP_TIMEOUT = 60.0
#: After a failed `graceful_stop` exec, how long a container still reported running may take to
#: go down before the failure counts.
EXEC_EXIT_GRACE = 5.0


def graceful_stop_for(service: str, env=None, services_dir=None) -> tuple[str | None, float]:
    """(command, timeout) from the service's `graceful_stop`, or (None, 0.0) when it has none."""
    defn = registry.load_service(service, services_dir=services_dir,
                                 env=os.environ if env is None else env)
    if not defn.graceful_stop:
        return None, 0.0
    return defn.graceful_stop, defn.graceful_stop_timeout or GRACEFUL_STOP_TIMEOUT


#: Wait for a service's `restart_in_place` to return, when it sets no timeout.
RESTART_IN_PLACE_TIMEOUT = 900.0


def restart_in_place_for(service: str, env=None, services_dir=None) -> tuple[str | None, float]:
    """(command, timeout) from the service's `restart_in_place`, or (None, 0.0) when it has none."""
    defn = registry.load_service(service, services_dir=services_dir,
                                 env=os.environ if env is None else env)
    if not defn.restart_in_place:
        return None, 0.0
    return defn.restart_in_place, defn.restart_in_place_timeout or RESTART_IN_PLACE_TIMEOUT


def hooks_for(service: str, signal: str, env=None, services_dir=None):
    """(graceful, restart) for `cycle`: the service's graceful_stop under TERM (None if it has
    none), its restart_in_place under RESTART (OutageError if it has none). Each is looked up
    only for its own signal."""
    if signal == "TERM":
        cmd, timeout = graceful_stop_for(service, env=env, services_dir=services_dir)
        return ((cmd, timeout) if cmd else None), None
    if signal == "RESTART":
        cmd, timeout = restart_in_place_for(service, env=env, services_dir=services_dir)
        if not cmd:
            raise OutageError(f"service_outage: {service} has no restart_in_place")
        return None, (cmd, timeout)
    return None, None


def _tick(progress, t0, total):
    if progress:
        try:
            progress(time.monotonic() - t0, total)
        except Exception:
            pass


def _running(container: str, run=None) -> bool:
    r = _sh(["docker", "container", "inspect", "-f", "{{.State.Running}}", container], run=run)
    return (getattr(r, "stdout", "") or "").strip() != "false"


def _goes_down(container: str, within: float, poll: float, run=None) -> bool:
    deadline = time.monotonic() + within
    while _running(container, run):
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


def _kill_and_raise(container: str, why: str, run=None):
    r = _sh(["docker", "kill", "-s", "KILL", container], run=run)
    if getattr(r, "returncode", 1) != 0:
        why += f"; docker kill failed too: {(getattr(r, 'stderr', '') or '').strip()!r}"
    else:
        why += "; KILLed"
    raise OutageError(why)


def interrupt(container: str, signal: str, run=None, verify_timeout: float = 30.0,
              poll: float = 1.0, report=None, progress=None,
              graceful: tuple[str, float] | None = None) -> None:
    """KILL: `docker kill -s KILL`. TERM: `docker stop`, which sends the image's STOPSIGNAL
    (postgres: SIGINT, a fast shutdown) and KILL after the grace period; or, with `graceful`
    (the service's graceful_stop command and timeout), that command via `docker exec`, then a
    wait of up to its timeout for the container to exit. A graceful stop that does not finish
    is KILLed and raises, so the test never passes on a stop it did not ask for.
    Raises OutageError unless the container is confirmed not running."""
    if signal == "KILL":
        argv = ["docker", "kill", "-s", "KILL", container]
    elif signal == "TERM" and graceful:
        argv = ["docker", "exec", container, "sh", "-c", graceful[0]]
        verify_timeout = graceful[1]
    elif signal == "TERM":
        argv = ["docker", "stop", container]
    else:
        raise OutageError(f"unknown signal {signal!r}")
    _say(report, f"service_outage: {' '.join(argv[1:])}")
    exec_ = argv[1] == "exec"
    try:
        r = _sh(argv, run=run, timeout=verify_timeout if exec_ else None)
    except subprocess.TimeoutExpired:
        _kill_and_raise(container, f"graceful_stop in {container} did not return in "
                                   f"{verify_timeout:.0f}s", run=run)
    # The exec can lose its container as the server exits; that is the stop succeeding.
    if getattr(r, "returncode", 1) != 0 and (
            not exec_ or not _goes_down(container, EXEC_EXIT_GRACE, poll, run)):
        raise OutageError(f"{argv[1]} {container} failed: "
                          f"{(getattr(r, 'stderr', '') or '').strip()!r}")
    t0 = time.monotonic()
    deadline = t0 + verify_timeout
    while True:
        _tick(progress, t0, verify_timeout)
        if not _running(container, run):
            return
        if time.monotonic() >= deadline:
            if exec_:
                _kill_and_raise(container, f"{container} still running {verify_timeout:.0f}s "
                                           f"after its graceful_stop", run=run)
            raise OutageError(f"{container} still running {verify_timeout:.0f}s after {argv[1]}")
        time.sleep(poll)


def _tail(*streams, lines: int = 20) -> str:
    """The last lines of the first non-empty stream (str or bytes), for a failure message."""
    for s in streams:
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        s = (s or "").strip()
        if s:
            return "\n".join(s.splitlines()[-lines:])
    return ""


def restart_in_place(container: str, hook: tuple[str, float], run=None, poll: float = 2.0,
                     report=None, progress=None) -> None:
    """Run the service's restart_in_place command (`docker exec`) and wait up to its timeout for
    it to return; it exits 0 once the server is back. A failure or a hang raises. The container
    is never stopped or KILLed here: a KILL can damage what it hosts (a database VM)."""
    cmd, timeout = hook
    argv = ["docker", "exec", container, "sh", "-c", cmd]
    _say(report, f"service_outage: {' '.join(argv[1:])}")
    box = {}

    def _go():
        try:
            box["r"] = _sh(argv, run=run, timeout=timeout)
        except BaseException as e:
            box["e"] = e

    t0 = time.monotonic()
    th = threading.Thread(target=_go, name="restart_in_place", daemon=True)
    th.start()
    while th.is_alive():
        _tick(progress, t0, timeout)
        th.join(poll)
    took = time.monotonic() - t0
    left = f"{container} was left running, not KILLed; check it by hand before the next test"
    if isinstance(box.get("e"), subprocess.TimeoutExpired):
        e = box["e"]
        out = _tail(e.stderr, e.stdout)
        raise OutageError(f"restart_in_place in {container} did not return in {timeout:.0f}s "
                          f"(HANG; the hook may still be running in it); {left}"
                          + (f"; its output so far:\n{out}" if out else ""))
    if "e" in box:
        raise box["e"]
    r = box["r"]
    rc = getattr(r, "returncode", 1)
    if rc != 0:
        out = _tail(getattr(r, "stderr", ""), getattr(r, "stdout", ""))
        raise OutageError(f"restart_in_place in {container} failed (exit {rc}) after {took:.0f}s; "
                          f"{left}; its output:\n{out}")
    _say(report, f"service_outage: {container} restarted in place in {took:.0f}s")


def restore(container: str, run=None, ready_timeout: float = 120.0, poll: float = 2.0,
            report=None, progress=None) -> None:
    """`docker start`, then wait for healthy (or running + 5 s with no healthcheck)."""
    _say(report, f"service_outage: start {container}")
    r = _sh(["docker", "start", container], run=run)
    if getattr(r, "returncode", 1) != 0:
        raise OutageError(f"docker start {container} failed: "
                          f"{(getattr(r, 'stderr', '') or '').strip()!r}")
    t0 = time.monotonic()
    deadline = t0 + ready_timeout
    last = "unknown"
    while True:
        _tick(progress, t0, ready_timeout)
        r = _sh(["docker", "container", "inspect", "-f",
                 "{{.State.Health.Status}}|{{.State.Running}}", container], run=run)
        out = (getattr(r, "stdout", "") or "").strip()
        health, _, running = out.partition("|")
        if out:
            last = f"health={health or '<none>'} running={running or '<none>'}"
        if health == "healthy":
            return
        # No healthcheck: Go templates print "<no value>" (older docker: empty).
        if health in ("", "<no value>") and running == "true":
            _say(report, f"service_outage: {container} has no healthcheck; waiting 5s")
            time.sleep(5.0)
            return
        if time.monotonic() >= deadline:
            raise OutageError(f"{container} not ready {ready_timeout:.0f}s after docker start "
                              f"(last status {last})")
        time.sleep(poll)


def cycle(container: str, signal: str, down_for: float, ready_timeout: float, wait,
          run=None, report=None, progress=None, graceful: tuple[str, float] | None = None,
          restart: tuple[str, float] | None = None) -> None:
    """Interrupt, `wait(down_for)`, restore. A failure before the restore still attempts one
    (`docker start` is idempotent) so the shared container is not left down; if that fails too
    it is reported and the original error propagates. RESTART instead runs `restart` (the
    service's restart_in_place) and nothing else: the container never stops."""
    if signal == "RESTART":
        if not restart:
            raise OutageError(f"signal RESTART needs the service's restart_in_place ({container})")
        restart_in_place(container, restart, run=run, report=report, progress=progress)
        return
    restoring = False
    try:
        interrupt(container, signal, run=run, report=report, progress=progress,
                  graceful=graceful)
        _say(report, f"service_outage: {container} down")
        wait(down_for)
        restoring = True
        restore(container, run=run, ready_timeout=ready_timeout, report=report,
                progress=progress)
    except BaseException as e:
        if not restoring:
            try:
                restore(container, run=run, ready_timeout=ready_timeout, report=report,
                        progress=progress)
            except Exception as e2:
                msg = f"service_outage: restore after the failure also failed: {e2}"
                _say(report, msg)
                e.add_note(msg)
        raise


def watch_app(client, app: str, settle: float, poll: float = 2.0, report=None) -> str:
    """Re-read `app` status for `settle` seconds; raise if it leaves RUNNING/COMPLETED."""
    deadline = time.monotonic() + settle
    now = client.current_status(app)
    while True:
        if now not in ("RUNNING", "COMPLETED"):
            raise OutageError(f"{app} left RUNNING during the outage settle (now {now})")
        if now == "COMPLETED":
            _say(report, "service_outage: app COMPLETED during the settle; done waiting")
            return now
        left = deadline - time.monotonic()
        if left <= 0:
            return now
        time.sleep(min(poll, left))
        now = client.current_status(app)
