"""Bounded Docker (and local probe) commands, shared by the live and integration engines.

A hung Docker daemon used to hang whatever called it: a declaration holding the port's registry lock, a
session teardown, every polling read of a server file. Each command now has a named bound, and a command
that outlives it is stopped and answers like a failed one (exit status ``TIMED_OUT``, a stderr that says
so), so every caller's existing failure path reports it.

Builds whose output streams to the terminal (``striim_provision._stream``: the installer download, the
Striim image build) are the one exception: the user watches them progress and can stop them. The cluster's
own `compose up`/`down` stream too, but through ``stream`` below, under the compose bounds.
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time

# `docker ps`, `inspect`, `images`, `cp` of a small file, `exec` of a read, a listing, a checksum or a
# removal, and local probes (`java -version`, `openssl`). The bound the ownership ledger has used for
# every one of its docker commands.
QUERY_TIMEOUT_S = 60.0
# `docker compose down -v`: stops each container (10 s grace by default, in parallel) and removes its
# volumes, which run to gigabytes for Oracle and SQL Server.
COMPOSE_DOWN_TIMEOUT_S = 600.0
# `docker compose up -d --wait [--build]`, and a service's post-up script: an image pull or build, then
# the healthcheck wait (up to about 17 minutes for the slowest native service, and about 2 hours for a
# Teradata boot under emulation, which the integration compose lock has waited out).
COMPOSE_UP_TIMEOUT_S = 10800.0
# A build whose output is captured, not streamed (a Maven OP/UDF build, the GoldenGate harness javac):
# a cold local Maven repository downloads its dependencies first.
BUILD_TIMEOUT_S = 3600.0
# How long a process waits for a bring-up lock another process holds: the holder's reset, bring-up and
# post-up are each bounded above, so a waiter outlives any holder that is still making progress.
LOCK_WAIT_S = COMPOSE_DOWN_TIMEOUT_S + 2 * COMPOSE_UP_TIMEOUT_S

TIMED_OUT = 124                 # the exit status `timeout(1)` uses


def timed_out_message(argv, timeout: float) -> str:
    shown = " ".join(str(a) for a in list(argv)[:3])
    hint = " (is the Docker daemon responding?)" if argv and str(argv[0]) == "docker" else ""
    return f"`{shown} ...` did not finish within {timeout:.0f}s and was stopped{hint}"


class DockerTimeout(subprocess.CalledProcessError):
    """A ``check=True`` command that outlived its bound. A CalledProcessError, so callers that already
    handle a failed command handle this one; its text says it timed out."""

    def __init__(self, argv, timeout, stdout=None):
        super().__init__(TIMED_OUT, argv, stdout, timed_out_message(argv, timeout))
        self.timeout = timeout

    def __str__(self):
        return self.stderr


def run(argv, *, timeout: float = QUERY_TIMEOUT_S, check: bool = False, capture: bool = True, **kw):
    """``subprocess.run`` with a bound. On expiry the command is killed and a CompletedProcess with exit
    status ``TIMED_OUT`` is returned (``check=True``: ``DockerTimeout`` is raised). ``capture=False``
    leaves stdout and stderr on the terminal. Missing binaries raise FileNotFoundError, as before.

    On expiry and on any exception (Ctrl-C included) the command is killed with every process it started,
    those whose parent has already exited included: `docker compose` runs as a child of `docker`, and a
    post-up script or a build starts children of its own, so killing only the direct child left them
    running -- a `down -v` that went on after its caller had given up, or a bring-up that kept creating
    what cleanup had removed. The command runs in a process group of its own (``_spawn``), which is also
    killed when the process that started it dies, however it dies: a tier that striim-test stops or kills
    takes its commands with it."""
    if capture:
        kw.setdefault("capture_output", True)
        kw.setdefault("text", True)
    try:
        r = _run_group(argv, timeout, kw)
    except subprocess.TimeoutExpired as e:
        if check:
            raise DockerTimeout(argv, timeout, e.stdout) from None
        return subprocess.CompletedProcess(argv, TIMED_OUT, "" if capture else None,
                                           timed_out_message(argv, timeout))
    if check and r.returncode:
        raise subprocess.CalledProcessError(r.returncode, argv, r.stdout, r.stderr)
    return r


_SUBPROCESS_RUN = subprocess.run

# Holds the command's process group (``_spawn``). It waits for end-of-file on its stdin, a pipe that only
# the process that started the command holds open, and then kills its own group: the command and every
# process it started. Signals that would end it without doing that are ignored.
_ANCHOR = ["/bin/sh", "-c", "trap '' HUP INT QUIT TERM; read -r line; kill -s KILL 0"]


def _spawn(argv, **kw):
    """Start ``argv`` in a new process group; return ``(proc, anchor)``.

    The group belongs to ``anchor``, a process started first in a group of its own, which the command
    joins. A process keeps its group when its parent exits, so killing the group reaches every process the
    command started however much of its ancestry is gone (one that leaves the group itself, with
    ``setsid`` or ``setpgid``, is out of reach, as it is of a terminal's job control).

    The group id is the anchor's pid, and only ``_kill_group`` or ``_release`` reaps the anchor, in the
    thread that started the command, after the group has been signalled. Until then the kernel gives that
    pid, and so that group id, to nothing else: the group the cleanup signals is always this command's. The
    command's own pid is never signalled once the command may have been reaped.

    The command is not in the caller's process group, so a signal to that group (striim-test cancelling a
    tier, a Ctrl-C at the terminal) reaches the caller, not the command. The caller's interrupt stops it
    (``run`` and ``stream`` kill the group on any exception); if the caller dies instead, the anchor's stdin
    reaches end-of-file and the anchor kills the group."""
    anchor = subprocess.Popen(_ANCHOR, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, process_group=0)
    try:
        proc = subprocess.Popen(argv, process_group=anchor.pid, **kw)
    except BaseException:
        _kill_group(anchor, None)
        raise
    return proc, anchor


def _kill_group(anchor, proc) -> None:
    """SIGKILL the command's process group, then reap the anchor. Sent twice more, a moment apart, for a
    process that was being forked as the first one landed."""
    if anchor.returncode is None:
        for _ in range(3):
            try:
                os.killpg(anchor.pid, signal.SIGKILL)
            except ProcessLookupError:
                break
            except PermissionError:     # macOS, while a killed group's members are being reaped
                pass
            time.sleep(0.05)
        try:
            anchor.kill()               # in case no group signal could be delivered
        except OSError:
            pass
    _reap_anchor(anchor)
    if proc is not None:
        try:
            proc.kill()                 # a command that left the group; a no-op once it has been reaped
        except OSError:
            pass


def _release(anchor) -> None:
    """The command has finished: end the anchor alone (SIGKILL, so it never reads the end-of-file that would
    kill the group). What the command left running is left as it is, as before."""
    try:
        anchor.kill()
    except OSError:
        pass
    _reap_anchor(anchor)


def _reap_anchor(anchor) -> None:
    anchor.wait()
    if anchor.stdin is not None:
        try:
            anchor.stdin.close()
        except OSError:
            pass


def _run_group(argv, timeout: float, kw):
    """``subprocess.run(argv, timeout=timeout, **kw)``, except that the command's whole process group is killed
    when the bound expires (``TimeoutExpired`` is then raised, as ``subprocess.run`` raises it) or when
    anything else interrupts the wait. A ``subprocess.run`` replaced in this process (a hermetic test's
    stand-in for the command) is called as before: there is no process here to kill."""
    if subprocess.run is not _SUBPROCESS_RUN:
        return subprocess.run(argv, timeout=timeout, **kw)
    kw = dict(kw)
    if kw.pop("capture_output", False):
        kw["stdout"] = kw["stderr"] = subprocess.PIPE
    stdin_data = kw.pop("input", None)
    if stdin_data is not None:
        kw["stdin"] = subprocess.PIPE
    proc, anchor = _spawn(argv, **kw)
    try:
        out, err = proc.communicate(stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(anchor, proc)
        raise subprocess.TimeoutExpired(argv, timeout, output=_drain(proc)) from None
    except BaseException:
        _kill_group(anchor, proc)
        try:
            _drain(proc)
        except Exception:               # noqa: BLE001 - the interrupt is what the caller must see
            proc.wait()
        raise
    _release(anchor)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _drain(proc):
    """Reap a killed command and return what it wrote to a captured stdout. Bounded: a process outside the
    group (one that detached itself) could still hold the pipe; its output is then not waited for."""
    try:
        out, _err = proc.communicate(timeout=5)
        return out
    except subprocess.TimeoutExpired:
        for f in (proc.stdin, proc.stdout, proc.stderr):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        proc.wait()
        return None


# How long a stream waits for the rest of the output once its command has finished or its group has been
# killed: only a process outside the group (one that detached itself) can hold the pipe open past that.
_STREAM_DRAIN_S = 5.0


def stream(argv, on_line, *, timeout: float, env=None, cwd=None) -> int:
    """Run ``argv`` with stdout and stderr merged, handing each line to ``on_line``; return its exit status,
    or ``TIMED_OUT`` when it outlived ``timeout``. As with ``run``, the command runs in a process group of
    its own, killed with every process it started on expiry, on any exception (Ctrl-C included) and when
    the caller dies. The bound holds whoever keeps the pipe open: a thread of its own reads the output, and
    this one waits for its lines against the deadline. ``cwd`` is the directory it runs in (None: this
    process's)."""
    deadline = time.monotonic() + timeout
    proc, anchor = _spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                          env=env, cwd=cwd)
    lines = queue.SimpleQueue()

    def read():
        try:
            for line in proc.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    reader = threading.Thread(target=read, daemon=True, name="slt-dockercmd-stream")
    reader.start()
    timed_out, eof = False, False
    try:
        while not eof:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                timed_out = True
                break
            if line is None:
                eof = True
            else:
                on_line(line)
        if not timed_out:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
    except BaseException:
        _kill_group(anchor, proc)
        proc.wait()
        raise
    if timed_out:
        _kill_group(anchor, proc)
    else:
        _release(anchor)
    proc.wait()
    reader.join(_STREAM_DRAIN_S)
    while not eof:                      # what the command wrote before it was stopped
        try:
            line = lines.get_nowait()
        except queue.Empty:
            break
        if line is None:
            eof = True
        else:
            on_line(line)
    if eof:
        proc.stdout.close()
    return TIMED_OUT if timed_out else proc.returncode
