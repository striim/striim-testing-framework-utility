"""The `seed_when: post_start` handshake — a cross-process rendezvous, tested as one.

A change stream captures only commits made after its start timestamp, so a streaming case has to
commit its rows while the reader is already running. That makes this the one place in the tier
where two processes have to agree on an ordering, and the failure modes are the ones every
subprocess handshake has: the child dying early, the callback raising, and the pipe buffer.
"""
import pathlib
import subprocess
import sys
import time

from unittest import mock

import pytest

from inttest.harness import _run_with_post_start_seed


def child(tmp_path, *, stderr_bytes=0, exit_code=0, signal_ready=True, sleep=0.0):
    """A stand-in driver: optionally signals readiness, writes stderr, then exits."""
    body = ""
    if signal_ready:
        body += f"import pathlib;pathlib.Path(r'{tmp_path}','seed.ready').write_text('');"
    body += f"import time;time.sleep({sleep});"
    if stderr_bytes:
        body += f"import sys;sys.stderr.write('x'*{stderr_bytes});sys.stderr.flush();"
    body += f"raise SystemExit({exit_code})"
    return [sys.executable, "-c", body]


def run(tmp_path, argv, *, on_started=None, timeout=20):
    return _run_with_post_start_seed(argv, cwd=None, timeout=timeout, tempdir=tmp_path,
                                     on_source_started=on_started, op_jar="fake.jar")


def test_the_callback_runs_and_the_child_is_released(tmp_path):
    calls = []
    argv = [sys.executable, "-c",
            f"import pathlib,time;p=pathlib.Path(r'{tmp_path}');"
            f"p.joinpath('seed.ready').write_text('');"
            # Block until released, exactly as the Java driver does.
            f"[time.sleep(0.02) for _ in iter(lambda: p.joinpath('seed.go').exists(), True)];"
            f"raise SystemExit(0)"]
    r = run(tmp_path, argv, on_started=lambda: calls.append("seeded"))
    assert r.returncode == 0
    assert calls == ["seeded"], "the seed must run exactly once, while the child waits"
    assert (tmp_path / "seed.go").exists()


@pytest.mark.parametrize("size", [1_000, 70_000, 400_000])
def test_a_chatty_child_does_not_deadlock(tmp_path, size):
    """⚠ The regression this file exists for.

    An earlier revision used `stdout=PIPE, stderr=PIPE` and a poll loop that read neither until
    the child exited. A child writing past the ~64 KiB pipe buffer blocks in write() forever
    while this side spins to its deadline and reports a bogus timeout. Measured before the fix:
    65,000 bytes passed, 70,000 bytes deadlocked.

    It bit hardest where it hurt most -- a JVM stack trace with a deep gRPC cause chain is the
    largest output the driver ever produces, and it is produced on the FAILURE path, so a real
    operator failure became "the harness timed out".
    """
    started = time.monotonic()
    r = run(tmp_path, child(tmp_path, stderr_bytes=size), on_started=lambda: None, timeout=15)
    assert r.returncode == 0
    assert len(r.stderr) == size, "the child's whole output must be captured, not truncated"
    assert time.monotonic() - started < 10, "completed, not deadlocked-then-killed"


def test_a_child_that_dies_before_signalling_surfaces_its_own_failure(tmp_path):
    # ⚠ Not a handshake error. Raising one here would bury the real failure, which is the mistake
    # that makes a harness problem read as an operator bug.
    r = run(tmp_path, child(tmp_path, signal_ready=False, exit_code=3, stderr_bytes=200))
    assert r.returncode == 3
    assert len(r.stderr) == 200


def test_a_raising_callback_kills_and_reaps_the_child(tmp_path):
    """⚠ Asserts the KILL and the REAP, not merely that something was raised.

    `pytest.fail` raises `OutcomeException`, which derives from `BaseException` -- so a narrower
    `except Exception:` in the runner would let the exception past while leaving a 60-second child
    running. A review made exactly that mutation and this test PASSED, because it only checked
    that an exception escaped. Catching the process handle and asserting it is dead is the
    difference between testing the raise and testing the cleanup.
    """
    argv = [sys.executable, "-c",
            f"import pathlib,time;p=pathlib.Path(r'{tmp_path}');"
            f"p.joinpath('seed.ready').write_text('');time.sleep(60)"]

    spawned = []
    real_popen = subprocess.Popen

    def capturing_popen(*a, **kw):
        proc = real_popen(*a, **kw)
        spawned.append(proc)
        return proc

    def boom():
        pytest.fail("seed SQL failed")

    started = time.monotonic()
    with mock.patch.object(subprocess, "Popen", capturing_popen):
        with pytest.raises(BaseException):
            run(tmp_path, argv, on_started=boom, timeout=20)

    assert spawned, "the runner must have spawned a child for this test to mean anything"
    child = spawned[0]
    assert child.poll() is not None, "the child must be KILLED, not left running for 60s"
    assert time.monotonic() - started < 10, "must not have waited out the child's own sleep"


def test_a_child_that_never_finishes_times_out_with_its_output(tmp_path):
    argv = [sys.executable, "-c",
            f"import pathlib,sys,time;p=pathlib.Path(r'{tmp_path}');"
            f"sys.stderr.write('trace');sys.stderr.flush();"
            f"p.joinpath('seed.ready').write_text('');time.sleep(60)"]
    with pytest.raises(subprocess.TimeoutExpired) as e:
        run(tmp_path, argv, on_started=lambda: None, timeout=2)
    # The captured output must survive the timeout -- it is what says why.
    assert "trace" in (e.value.stderr or "")
