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

import collections
import os
import selectors
import signal
import subprocess
import time

# `docker ps`, `inspect`, `images`, `cp` of a small file, `exec` of a read, a listing, a checksum or a
# removal, and local probes (`java -version`, `openssl`). The bound the ownership ledger has used for
# every one of its docker commands.
QUERY_TIMEOUT_S = 60.0
# A copy whose size the case sets: `docker cp` of a `server_files` seed or an OP jar into (or out of) a
# container, and the `exec` that reads a FileWriter output back. Sized for a multi-gigabyte copy on a slow
# Docker Desktop, not for a query. The small command each of these follows (a listing, a mkdir, a
# checksum) keeps the query bound. Use ``transfer``.
TRANSFER_TIMEOUT_S = 1800.0
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

    Nothing the command started outlives the call. The command runs in a process group of its own
    (``_spawn``), and the group is killed when the call ends, however it ends: at the bound, on any
    exception (Ctrl-C included), and once the command has finished, which ends any helper it left running.
    That reaches every process it started, those whose parent has already exited included: `docker
    compose` runs as a child of `docker`, and a post-up script or a build starts children of its own.
    The group is also killed when the process that started it dies, however it dies: a tier that
    striim-test stops or kills takes its commands with it."""
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


def transfer(argv, **kw):
    """``run`` under ``TRANSFER_TIMEOUT_S``: the default runner for a copy or a read of a file of any size.
    Callers that take an injected ``run`` use it in place of this, called with the command alone."""
    return run(argv, timeout=TRANSFER_TIMEOUT_S, **kw)


# Holds the command's process group (``_spawn``). It waits for end-of-file on its stdin, a pipe that only
# the process that started the command holds open, and then kills its own group: the command and every
# process it started. Signals that would end it without doing that are ignored.
_ANCHOR = ["/bin/sh", "-c", "trap '' HUP INT QUIT TERM; read -r line; kill -s KILL 0"]


def _spawn(argv, **kw):
    """Start ``argv`` in a new process group; return ``(proc, anchor)``. The caller ends the group with
    ``_stop`` whatever happens.

    The group belongs to ``anchor``, a process started first in a group of its own, which the command
    joins. A process keeps its group when its parent exits, so killing the group reaches every process the
    command started however much of its ancestry is gone (one that leaves the group itself, with
    ``setsid`` or ``setpgid``, is out of reach, as it is of a terminal's job control).

    The command is not in the caller's process group, so a signal to that group (striim-test cancelling a
    tier, a Ctrl-C at the terminal) reaches the caller, not the command. The caller's interrupt stops it
    (``run`` and ``stream`` end the group on any exception); if the caller dies instead, the anchor's stdin
    reaches end-of-file and the anchor kills the group."""
    anchor = subprocess.Popen(_ANCHOR, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, process_group=0)
    try:
        proc = subprocess.Popen(argv, process_group=anchor.pid, **kw)
    except BaseException:
        _stop(anchor, None)
        raise
    return proc, anchor


def _stop(anchor, proc) -> None:
    """End the command's process group: SIGKILL to the group (the command if it is still running, what it
    started that is still in the group, the anchor), then reap the anchor and the command. A second call
    does nothing.

    The group id is the anchor's pid, and the anchor is reaped only here, after the last group signal:
    until then the kernel gives that pid, and so that group id, to nothing else, so the group signalled is
    always this command's. The signal is sent twice more, a moment apart, for a process that was being
    forked as the first one landed. The command itself is signalled only through its Popen, which does
    nothing once it has been reaped, so a pid the kernel has since handed on is never signalled."""
    if anchor.returncode is None:
        for attempt in range(3):
            if attempt:
                time.sleep(0.02)
            try:
                os.killpg(anchor.pid, signal.SIGKILL)
            except ProcessLookupError:
                break
            except PermissionError:     # macOS, while a killed group's members are being reaped
                pass
        try:
            anchor.kill()               # in case no group signal could be delivered
        except OSError:
            pass
        anchor.wait()
        anchor.stdin.close()
    if proc is not None:
        try:
            proc.kill()                 # a command that left the group
        except OSError:
            pass
        proc.wait()


def _run_group(argv, timeout: float, kw):
    """``subprocess.run(argv, timeout=timeout, **kw)``, ending the command's process group (``_stop``) once
    it has finished, when the bound expires (``TimeoutExpired`` is then raised, as ``subprocess.run``
    raises it) and when anything else interrupts the wait."""
    kw = dict(kw)
    if kw.pop("capture_output", False):
        kw["stdout"] = kw["stderr"] = subprocess.PIPE
    stdin_data = kw.pop("input", None)
    if stdin_data is not None:
        kw["stdin"] = subprocess.PIPE
    proc, anchor = _spawn(argv, **kw)
    expired = False
    try:
        out, err = proc.communicate(stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        expired = True
    finally:
        _stop(anchor, proc)
    if expired:
        raise subprocess.TimeoutExpired(argv, timeout, output=_drain(proc))
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _drain(proc):
    """What a stopped command wrote to a captured stdout. Bounded: a process outside the group (one that
    detached itself) could still hold the pipe; its output is then not waited for."""
    try:
        return proc.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        for f in (proc.stdin, proc.stdout, proc.stderr):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        return None


# How long a stream stopped at its bound goes on handing over output that is still there: what the command
# wrote before it was stopped, and anything a process outside its group (one that detached itself) writes.
_LATE_S = 2.0


def stream(argv, on_line, *, timeout: float, env=None, cwd=None) -> int:
    """Run ``argv`` with stdout and stderr merged, handing each line to ``on_line``; return its exit status,
    or ``TIMED_OUT`` when it outlived ``timeout``. ``cwd`` is the directory it runs in (None: this
    process's). As with ``run``, the command runs in a process group of its own, ended with everything in
    it when the call ends: at the bound, on any exception (Ctrl-C included), once the command has finished,
    and when the caller dies.

    The bound is the call's. The deadline is checked before each line is handed over and the output is
    read only up to it, so neither a slow ``on_line`` nor a command that keeps writing carries the call
    past it, and a command that has not finished by then has timed out. What is still there once the
    command has been stopped is handed over for at most ``_LATE_S``; the last line then says if the rest
    was dropped."""
    deadline = time.monotonic() + timeout
    proc, anchor = _spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd)
    out = _Lines(proc.stdout.fileno())
    status = None
    with proc.stdout:
        try:
            for line in out.until(deadline):
                on_line(line)
            if out.done:
                status = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        finally:
            _stop(anchor, proc)
        if status is None:
            for line in out.until(time.monotonic() + _LATE_S):
                on_line(line)
            if not out.done:
                on_line(f"... the rest of the output is not shown: the command was stopped at its "
                        f"{timeout:.0f}s bound\n")
    return TIMED_OUT if status is None else status


class _Lines:
    """The lines read from a pipe, as text mode gives them (decoded, each ending as ``\\n``), read only
    while a deadline lasts."""

    def __init__(self, fd):
        self._fd, self._partial, self._lines, self._eof = fd, b"", collections.deque(), False

    @property
    def done(self) -> bool:
        """End-of-file, and every line handed over."""
        return self._eof and not self._lines

    def until(self, deadline):
        """Yield lines until end-of-file or ``deadline``, which is checked before each one."""
        with selectors.DefaultSelector() as sel:
            sel.register(self._fd, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if self._lines:
                    yield self._lines.popleft()
                elif self._eof:
                    return
                elif sel.select(deadline - time.monotonic()):
                    self._take(os.read(self._fd, 65536))

    def _take(self, chunk: bytes) -> None:
        self._eof = not chunk
        lines = (self._partial + chunk).splitlines(keepends=True)
        # An unfinished last line waits for the rest (a "\r" too: it may be the start of "\r\n").
        self._partial = lines.pop() if lines and not self._eof and not lines[-1].endswith(b"\n") else b""
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            text = line.rstrip("\r\n")
            self._lines.append(text + "\n" if text != line else line)
