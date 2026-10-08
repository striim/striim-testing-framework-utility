"""The live tier's crash/recovery phase: interrupt a running app, bring it back, and let the
test's own assertions say what survived.

WHY THIS EXISTS. Every other phase in the harness verifies a pipeline that ran to completion
undisturbed. A checkpoint can claim progress that no target durably made, leaving
in-flight events unreplayed after interruption. Recovery cases exercise that risk and
compare the durable output after restarting the app.

WHAT IT DOES NOT DO. It does not decide whether data was lost. It restores the app and gets
out of the way; `assert.data` / `assert.diff` do the judging. That separation is deliberate:
a recovery test's interesting assertion is almost always a COMPARISON the existing assertion
vocabulary already expresses (source vs target, or two fan-out siblings against each other),
and a bespoke "did we lose anything" checker would duplicate it less well.

THE THREE MODES ARE NOT INTERCHANGEABLE. See VALID_RECOVER_MODES in manifest.py. `stop` and
`quiesce` differ by exactly one platform behaviour -- `Flow.stopImpl()` has no flush() call
and `Flow.quiesceFlush()` does -- which makes them a matched pair: run the same manifest
under both and the delta isolates the shutdown path from the pipeline.
"""
from __future__ import annotations

import subprocess
import os
import time

from livetest import stack

# Statuses from which a START is meaningful. A `stop` leaves the app DEPLOYED (or CREATED if
# the platform undeployed it); a `kill` leaves whatever the dying JVM last persisted, and the
# platform's own auto-restart may already have moved it on.
_RESTARTABLE = {"CREATED", "DEPLOYED", "STOPPED", "QUIESCED",
                # A SIGKILLed app very plausibly comes back CRASH or HALT. Omitting these sent
                # `kill` down the "wait for the platform" branch, where await_running raises
                # "reached terminal status CRASH" instead of issuing the START that recovers it.
                "CRASH", "HALT"}

# Statuses the platform is still moving through. STOP and QUIESCE are ASYNCHRONOUS -- the API
# returns when the command is accepted, not when it has taken effect -- so these are the states
# an immediate status read is most likely to catch. `undeploy_application` in
# tools/python/striim_api.py carries its own 3x20s retry loop for STOPPING, which is the
# in-tree evidence that this is a real transient rather than a theoretical one.
# Every name here is a member of com.webaction.runtime.meta.MetaInfo$StatusInfo$Status,
# checked against the platform rather than written from memory -- an earlier revision of this set
# carried TERMINATED and UNDEPLOYING, neither of which the platform defines, so those entries
# could never match and the quiesce states that DO exist were missing instead.
#
# THE QUIESCE PATH IS WHY THIS LIST IS LONG. A plain QUIESCE moves
#   RUNNING -> APPROVING_QUIESCE -> QUIESCING -> STOPPING -> QUIESCED
# (design §4.2, read from the 5.4.0.6C handlers: QuiesceActionHandler answers
# APPROVING_QUIESCE, WaitApprovingQuiesceActionHandler sets QUIESCING, the
# WaitQuiescingFlushing/Checkpointing handlers set STOPPING on "App Manager completed QUIESCE",
# and AppManagerWorker maps that to QUIESCED). RUNNING_UNTIL_QUIESCE is what the QUIESCE action
# answers IN PLACE OF APPROVING_QUIESCE under QUIESCE_ON_IN_QUIESCE, and no handler assigns
# FLUSHING at all; both stay in
# the set because they are real enum members and the app is not settled in either. An earlier
# revision of this comment gave the path as "APPROVING_QUIESCE -> RUNNING_UNTIL_QUIESCE ->
# FLUSHING -> QUIESCED"; the set was right anyway, which is why nothing noticed.
# Omitting the intermediate states made `await_left_running` return the moment it read
# APPROVING_QUIESCE and log a VERIFIED interruption -- while the app was still processing
# events. `restore()` then found a status that is not restartable, waited for the platform,
# and polled until the test timed out with no START ever issued. `quiesce` is the control arm
# for `stop`, so silently not working there would have invalidated the comparison the pair
# exists to make.
_TRANSITIONAL = {"STOPPING", "STARTING", "DEPLOYING",
                 "QUIESCING", "APPROVING_QUIESCE", "RUNNING_UNTIL_QUIESCE", "FLUSHING"}

# Statuses that resolve on their own once the cluster is whole again, so a START would race the
# platform rather than help it. NOT_ENOUGH_SERVERS is the state a `kill` leaves behind while the
# node it killed is still rejoining; it is not terminal, so await_running will sit on it, which
# is the correct behaviour -- it just must not be mistaken for restartable.
_SELF_HEALING = {"NOT_ENOUGH_SERVERS", "RECOVERING_SOURCES", "STARTING_SOURCES",
                 "VERIFYING_STARTING"}


class RecoveryError(Exception):
    pass


def _sh(argv, run=None):
    run = run or (lambda a: subprocess.run(a, capture_output=True, text=True))
    return run(argv)


def supported(mode: str, ctx) -> tuple[bool, str]:
    """Whether `mode` can run against this context. Returns (ok, reason).

    `kill` needs containers to kill. A native Striim (ctx.mode != "docker") has none, and
    SIGKILLing the operator's own long-lived install would be both wrong and unrecoverable
    by this harness -- so the test SKIPS rather than silently downgrading to `stop`, which
    exercises a different code path and would report a pass for a case never run.
    """
    if mode == "kill" and getattr(ctx, "mode", None) != "docker":
        return False, ("recover.mode 'kill' needs the Docker cluster (it SIGKILLs the app "
                       f"node container); this run is mode={getattr(ctx, 'mode', None)!r}")
    return True, ""


def await_left_running(client, app: str, timeout: float, poll: float = 2.0,
                       report=None) -> str:
    """Block until `app` has actually left RUNNING, and return the status it settled on.

    This is the step that makes an interruption *verified* rather than merely *requested*.
    `stop_application` is a bare DELETE and `QUIESCE` a TQL line; both return on acceptance, so
    a status read taken straight afterwards commonly still says RUNNING, or says STOPPING. Both
    readings used to break `restore()`: RUNNING skipped the START and then tripped the settle
    guard, and STOPPING is not restartable so it waited for a state the platform never leaves.

    It also supplies the guarantee `stop_app`/`quiesce_app` cannot give on their own --
    `striim_api` never calls `raise_for_status()`, so a rejected command returns quietly. An
    app still RUNNING when this times out means the interruption did not happen, which is the
    one failure a recovery test must never absorb silently.
    """
    deadline = time.monotonic() + timeout
    status = client.current_status(app)
    while status == "RUNNING" or status in _TRANSITIONAL:
        if time.monotonic() >= deadline:
            raise RecoveryError(
                f"{app} was still {status} {timeout:.0f}s after the interruption was issued -- "
                f"treating it as not interrupted rather than proceeding, because an "
                f"un-interrupted run would pass and prove nothing")
        if report:
            try:
                report(f"recover: waiting for app to stop (now {status})")
            except Exception:
                pass
        time.sleep(poll)
        status = client.current_status(app)
    return status


def _await_left_status(client, app: str, from_status: str, timeout: float,
                       poll: float = 2.0):
    """Poll until `app` reports something other than `from_status`, and return it.

    Returns None on timeout rather than raising: the caller's own `await_running` is the real
    gate, and this is only here to stop a still-unchanged terminal status (CRASH/HALT after a
    START) being read as a failure. Turning a slow-but-working START into an error would be the
    opposite of the point.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.current_status(app)
        if status != from_status:
            return status
        time.sleep(poll)
    return None


def interrupt(client, ctx, app: str, mode: str, run=None, report=None,
              verify_timeout: float = 120.0, poll: float = 2.0) -> None:
    """Interrupt `app` by `mode`. Raises RecoveryError if the interruption itself failed.

    Not best-effort, unlike teardown: an interruption that quietly did not happen turns the
    whole test into an ordinary undisturbed run that passes and proves nothing. That is the
    one failure mode a recovery test must never have, so every path here raises.

    For `stop`/`quiesce` the command is asynchronous, so this also WAITS for the app to leave
    RUNNING before returning -- the verification belongs here, at the point where the claim
    "the app was interrupted" is made. `kill` cannot be verified this way: the node hosting the
    status endpoint is the thing being killed.
    """
    def _say(msg):
        if report:
            try:
                report(msg)
            except Exception:
                pass

    if mode == "kill":
        nodes = stack.app_nodes()
        if not nodes:
            raise RecoveryError("recover.mode 'kill': no app node containers to kill")
        _say(f"recover: SIGKILL {' '.join(nodes)}")
        # -s KILL, not `docker stop`: stop sends SIGTERM first, which lets the JVM run
        # shutdown hooks and is therefore the graceful path this mode exists to avoid.
        r = _sh(["docker", "kill", "-s", "KILL", *nodes], run=run)
        if getattr(r, "returncode", 1) != 0:
            raise RecoveryError(
                f"docker kill failed: {(getattr(r, 'stderr', '') or '').strip()!r}")
        return

    if mode == "stop":
        _say("recover: STOP APPLICATION")
        client.stop_app(app)
        _say(f"recover: app is {await_left_running(client, app, verify_timeout, poll=poll, report=report)}")
        return

    if mode == "quiesce":
        _say("recover: QUIESCE APPLICATION")
        client.quiesce_app(app)
        _say(f"recover: app is {await_left_running(client, app, verify_timeout, poll=poll, report=report)}")
        return

    raise RecoveryError(f"unknown recover.mode {mode!r}")


def _restore_budget(default: float = 600.0) -> float:
    """SLT_RECOVER_RESTORE_TIMEOUT in seconds; a value that is not a positive number keeps the
    default rather than turning the restore into an instant timeout or a crash before the mode
    branch."""
    raw = os.environ.get("SLT_RECOVER_RESTORE_TIMEOUT")
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def restore(client, ctx, app: str, mode: str, timeout: int, settle: float = 0.0,
            expect_running: bool = True, run=None, report=None, progress=None,
            poll: float = 2.0) -> str:
    """Bring `app` back to RUNNING after `interrupt`, then pause `settle` seconds.

    Returns the final status. The `settle` pause is not politeness: recovery replays
    asynchronously, so an assertion that fires the instant the app reports RUNNING measures a
    half-replayed target. Without it a recovery test reports loss that is only lag, which is
    worse than no test -- it is a test that cries wolf.
    """
    def _say(msg):
        if report:
            try:
                report(msg)
            except Exception:
                pass

    # ⚠ The restore's own budget, not the manifest's. The manifest timeout is sized for the
    # REPLAY (a customer-trail arm carries 5400s); handing it to the cluster wait and then to the
    # RUNNING wait let a restore that was never going to succeed sit for two full timeouts --
    # measured: three hours on an app that did not come back after a SIGKILL, with nothing said
    # until the operator killed the run. A cluster is back within minutes or not at all, and an
    # app under RECOVERY restarts within seconds of its node; ten minutes for each is generous.
    budget = min(float(timeout), _restore_budget())

    if mode == "kill":
        # The container is dead. `docker start` it, re-authenticate (the API token died with
        # the JVM -- see striim_provision._reauthenticate for why a stale token reads as a
        # cluster timeout), and wait for the cluster to accept commands again.
        nodes = stack.app_nodes()
        _say(f"recover: restarting {' '.join(nodes)}")
        # NOT `docker start` first: restart_app_nodes below runs `docker restart`, which starts a
        # stopped container on its own. Doing both meant the second call SIGTERMed an
        # already-running node -- a graceful interruption injected into the very replay this
        # phase exists to observe, plus a doubled startup wait.
        from livetest import striim_provision
        # Clears the OP registry too: the ModuleClassLoaders this test's jars registered
        # lived only in the JVM that was just killed, while the MDR still lists the library.
        # A later test trusting the registry would deploy against nothing.
        striim_provision.restart_app_nodes(client, run=run, timeout=int(budget))

    if not expect_running:
        status = client.current_status(app)
        _say(f"recover: app is {status}; expect_running is false, not starting it")
        return status

    # An app under RECOVERY may restart itself once its node is back, so a START would race
    # with the platform. Ask first, and only start from a state where starting is meaningful.
    status = client.current_status(app)
    if status != "RUNNING":
        if status in _RESTARTABLE:
            _say(f"recover: app is {status}; START APPLICATION")
            client.start_app(app)
            # START is ASYNCHRONOUS, exactly as STOP is, so the next read can still return the
            # status we started FROM. That matters only because two restartable statuses --
            # CRASH and HALT -- are also in striim.TERMINAL_STATUSES: handing either straight to
            # await_running makes it raise "reached terminal status CRASH" on the very state the
            # START was issued to leave. This is the `kill` path's normal case, not an edge one.
            # Waiting for the status to CHANGE is enough; await_running then does the real work.
            left = _await_left_status(client, app, status, timeout=min(60.0, budget), poll=poll)
            if left is not None:
                _say(f"recover: START accepted; app is now {left}")
        elif status in _SELF_HEALING:
            _say(f"recover: app is {status}; it resolves without a START, waiting")
        else:
            _say(f"recover: app is {status}; waiting for the platform to bring it back")
    try:
        client.await_running(app, timeout=budget, progress=progress)
    except Exception as e:
        # Name the state the app is stuck in: "did not reach RUNNING" alone sends the operator
        # to the target's data when the cause is the app never restarting. The status read is
        # guarded -- a node that never came back raises here too, and that must not replace the
        # error that explains it. The cause leads: a terminal status is not a timeout.
        try:
            now = client.current_status(app)
        except Exception as status_error:      # noqa: BLE001 -- the node itself is the news
            now = f"unreadable ({status_error})"
        raise RecoveryError(
            f"{app} did not come back after the restore: {e} (status now {now}; the wait was "
            f"{budget:.0f}s -- SLT_RECOVER_RESTORE_TIMEOUT raises it if the cluster genuinely "
            f"needs longer)") from e

    if settle > 0:
        _say(f"recover: settling {settle:.0f}s for replay to drain")
        deadline = time.monotonic() + settle
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            # Re-read the status while settling: an app that recovers into a HALT would
            # otherwise be discovered only by the assertions timing out, which reports the
            # symptom (no data) and hides the cause (the app died again).
            now = client.current_status(app)
            # COMPLETED is a SUCCESS status, not a death: a non-CDC source (DatabaseReader doing
            # an initial load) finishes its input and the app completes. Treating it as a failure
            # would make this phase unusable for any bounded source, and the settle exists to let
            # replay drain -- which a COMPLETED app has, by definition, finished doing.
            if now not in ("RUNNING", "COMPLETED"):
                raise RecoveryError(
                    f"{app} left RUNNING during the post-recovery settle (now {now})")
            if now == "COMPLETED":
                _say("recover: app COMPLETED during the settle (bounded source); done waiting")
                break
            time.sleep(min(2.0, left))

    return client.current_status(app)
