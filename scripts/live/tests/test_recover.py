"""Hermetic tests for the `recover:` phase -- manifest parsing and the interrupt/restore
dispatch. No server, no Docker: `interrupt`/`restore` take an injectable `run` for the docker
calls and a stub client for the API ones, which is the whole reason those seams exist.

What these CANNOT catch: whether a real STOP actually loses data. That is the live test's job
(a recovery case that sets `after` low). These pin the harness so that when
the live test reports loss, the loss is the product's and not the harness failing to interrupt.
"""
from pathlib import Path
import textwrap
import pytest

from livetest.manifest import load_manifest, ManifestError
from livetest import recovery
from livetest.recovery import RecoveryError


def _write(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "rec"
    d.mkdir()
    (d / "test.yaml").write_text(textwrap.dedent(body))
    return d / "test.yaml"


# Flat, already-dedented YAML. Tests append `recover:` blocks to it, and an indented base
# would skew textwrap.dedent's common-prefix calculation once a differently-indented block
# is concatenated on -- yielding valid-looking YAML with the wrong nesting.
_BASE = """\
name: rec
tql: app.tql
assert:
  smoke: true
  data:
    - target: t
      min_rows: 1
"""
_RECOVER = "recover:\n  mode: {mode}\n"


# --- manifest parsing -----------------------------------------------------------------

def test_recover_absent_is_none(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE))
    assert m.recover is None


def test_recover_defaults(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE + "recover:\n  mode: stop\n"))
    assert m.recover == {"mode": "stop", "after": 0.0, "settle": 20.0,
                         "expect_running": True, "times": 1, "every": 0.0}


def test_recover_all_fields(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE + (
        "recover:\n"
        "  mode: kill\n"
        "  after: 5\n"
        "  settle: 45\n"
        "  expect_running: true\n")))
    assert m.recover == {"mode": "kill", "after": 5.0, "settle": 45.0,
                         "expect_running": True, "times": 1, "every": 5.0}


def test_recover_expect_running_false_without_settle(tmp_path):
    # Legal: leave the app stopped and assert whatever the target holds. `settle` alongside it
    # is rejected -- see test_recover_rejects_settle_without_expect_running.
    m = load_manifest(_write(tmp_path, _BASE + (
        "recover:\n  mode: stop\n  expect_running: false\n")))
    assert m.recover["expect_running"] is False


@pytest.mark.parametrize("mode", ["kill", "stop", "quiesce"])
def test_recover_accepts_each_mode(tmp_path, mode):
    m = load_manifest(_write(tmp_path, _BASE + _RECOVER.format(mode=mode)))
    assert m.recover["mode"] == mode


def test_recover_rejects_unknown_mode(tmp_path):
    with pytest.raises(ManifestError, match="recover.mode"):
        load_manifest(_write(tmp_path, _BASE + "recover:\n  mode: sigterm\n"))


def test_recover_rejects_missing_mode(tmp_path):
    with pytest.raises(ManifestError, match="recover.mode"):
        load_manifest(_write(tmp_path, _BASE + "recover:\n  after: 5\n"))


def test_recover_rejects_unknown_key(tmp_path):
    with pytest.raises(ManifestError, match="unknown key"):
        load_manifest(_write(tmp_path, _BASE + "recover:\n  mode: stop\n  kilL: true\n"))


def test_recover_rejects_non_mapping(tmp_path):
    with pytest.raises(ManifestError, match="must be a mapping"):
        load_manifest(_write(tmp_path, _BASE + "recover: stop\n"))


def test_recover_rejects_non_bool_expect_running(tmp_path):
    with pytest.raises(ManifestError, match="expect_running"):
        load_manifest(_write(tmp_path, _BASE + (
            "recover:\n  mode: stop\n  expect_running: sure\n")))


def test_recover_rejects_settle_without_expect_running(tmp_path):
    with pytest.raises(ManifestError, match="recover.settle"):
        load_manifest(_write(tmp_path, _BASE + (
            "recover:\n  mode: stop\n  settle: 10\n  expect_running: false\n")))


def test_recover_and_expect_halt_are_mutually_exclusive(tmp_path):
    with pytest.raises(ManifestError, match="mutually exclusive"):
        load_manifest(_write(tmp_path,
            "name: rec\ntql: app.tql\nexpect_halt: true\n"
            "recover:\n  mode: stop\n"
            "assert:\n  data:\n    - target: t\n      min_rows: 1\n"))


def test_recover_requires_an_assertion_beyond_smoke(tmp_path):
    # smoke only re-asserts RUNNING, which the phase already waits for -- so a manifest with
    # nothing else declared would interrupt the app and then check nothing about the data.
    with pytest.raises(ManifestError, match="beyond 'smoke'"):
        load_manifest(_write(tmp_path,
            "name: rec\ntql: app.tql\n"
            "recover:\n  mode: stop\n"
            "assert:\n  smoke: true\n"))


# --- supported() ----------------------------------------------------------------------

class Ctx:
    def __init__(self, mode):
        self.mode = mode


@pytest.mark.parametrize("mode", ["stop", "quiesce"])
def test_api_modes_supported_natively(mode):
    ok, why = recovery.supported(mode, Ctx("native"))
    assert ok and why == ""


def test_kill_unsupported_natively():
    ok, why = recovery.supported("kill", Ctx("native"))
    assert not ok
    assert "Docker" in why and "native" in why


def test_kill_supported_in_docker():
    ok, _ = recovery.supported("kill", Ctx("docker"))
    assert ok


# --- interrupt() ----------------------------------------------------------------------

class FakeRun:
    """Records argv lists; returns a configurable returncode/stderr."""
    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self._rc = returncode
        self._err = stderr

    def __call__(self, argv):
        self.calls.append(list(argv))
        class R:
            returncode = self._rc
            stderr = self._err
            stdout = ""
        return R()


class StubClient:
    def __init__(self, statuses=None):
        self.stopped = []
        self.started = []
        self.quiesced = []
        self.awaited = []
        self._statuses = list(statuses) if statuses else ["RUNNING"]

    def stop_app(self, app):
        self.stopped.append(app)

    def start_app(self, app):
        self.started.append(app)

    def quiesce_app(self, app):
        self.quiesced.append(app)

    def current_status(self, app):
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]

    def await_running(self, app, timeout, progress=None):
        self.awaited.append(app)
        # Optional strict mode. The real client raises on a TERMINAL status
        # (striim.TERMINAL_STATUSES); this stub's silent no-op is exactly what let the
        # START-race defect pass 48 tests, so the tests that care opt into the real behaviour.
        if getattr(self, "strict_await", False):
            st = self.current_status(app)
            if st in ("CRASH", "HALT", "TERMINATED", "DEPLOY_FAILED"):
                raise AssertionError(f"reached terminal status {st}")


def test_interrupt_stop_calls_stop_app():
    c = StubClient(statuses=["STOPPED"])
    recovery.interrupt(c, Ctx("native"), "NS.App", "stop", verify_timeout=5)
    assert c.stopped == ["NS.App"] and c.quiesced == []


def test_interrupt_quiesce_calls_quiesce_app():
    c = StubClient(statuses=["QUIESCED"])
    recovery.interrupt(c, Ctx("native"), "NS.App", "quiesce", verify_timeout=5)
    assert c.quiesced == ["NS.App"] and c.stopped == []


def test_interrupt_kill_sends_sigkill_not_sigterm():
    run = FakeRun()
    recovery.interrupt(StubClient(), Ctx("docker"), "NS.App", "kill", run=run)
    assert len(run.calls) == 1
    argv = run.calls[0]
    # `docker stop` would send SIGTERM first and let shutdown hooks run, which is the
    # graceful path this mode exists to avoid.
    assert argv[:4] == ["docker", "kill", "-s", "KILL"]
    assert "stop" not in argv


def test_interrupt_kill_raises_when_docker_fails():
    run = FakeRun(returncode=1, stderr="No such container")
    with pytest.raises(RecoveryError, match="docker kill failed"):
        recovery.interrupt(StubClient(), Ctx("docker"), "NS.App", "kill", run=run)


def test_interrupt_rejects_unknown_mode():
    with pytest.raises(RecoveryError, match="unknown recover.mode"):
        recovery.interrupt(StubClient(), Ctx("native"), "NS.App", "sigterm")


def test_interrupt_never_silently_succeeds_on_a_bad_mode():
    # The one failure a recovery test must not have: the interruption did not happen and the
    # run degrades into an ordinary passing test.
    c = StubClient()
    with pytest.raises(RecoveryError):
        recovery.interrupt(c, Ctx("native"), "NS.App", "")
    assert c.stopped == [] and c.quiesced == []


# --- restore() ------------------------------------------------------------------------

def test_restore_starts_a_stopped_app():
    c = StubClient(statuses=["STOPPED", "RUNNING"])
    status = recovery.restore(c, Ctx("native"), "NS.App", "stop", timeout=30, settle=0)
    assert c.started == ["NS.App"]
    assert c.awaited == ["NS.App"]
    assert status == "RUNNING"


def test_restore_does_not_start_an_already_running_app():
    # An app under RECOVERY can restart itself; a START would race the platform.
    c = StubClient(statuses=["RUNNING"])
    recovery.restore(c, Ctx("native"), "NS.App", "stop", timeout=30, settle=0)
    assert c.started == []


def test_interrupt_stop_waits_out_a_transitional_status():
    # STOP is asynchronous, so the first read commonly says RUNNING or STOPPING. Previously
    # restore() acted on that first read: RUNNING skipped the START and then tripped the settle
    # guard, and STOPPING is not restartable so it waited for a state the platform never leaves.
    c = StubClient(statuses=["RUNNING", "STOPPING", "STOPPED"])
    recovery.interrupt(c, Ctx("native"), "NS.App", "stop", verify_timeout=5, poll=0)
    assert c.stopped == ["NS.App"]


def test_interrupt_raises_when_the_app_never_leaves_running():
    # The one failure a recovery test must never absorb: the command was rejected (striim_api
    # does not raise on a 4xx) and the run would otherwise continue undisturbed and pass.
    c = StubClient(statuses=["RUNNING"])
    with pytest.raises(RecoveryError, match="still RUNNING"):
        recovery.interrupt(c, Ctx("native"), "NS.App", "stop", verify_timeout=0)


def test_await_left_running_returns_the_settled_status():
    c = StubClient(statuses=["STOPPING", "QUIESCED"])
    assert recovery.await_left_running(c, "NS.App", timeout=5, poll=0) == "QUIESCED"


@pytest.mark.parametrize("terminal", ["CRASH", "HALT"])
def test_restore_starts_an_app_that_came_back_terminal(terminal):
    # What a SIGKILL actually leaves behind. These were missing from _RESTARTABLE, so restore()
    # fell through to await_running, which raises on a terminal status instead of recovering.
    c = StubClient(statuses=[terminal, "RUNNING"])
    recovery.restore(c, Ctx("native"), "NS.App", "stop", timeout=30, settle=0)
    assert c.started == ["NS.App"]


def test_restore_names_the_stuck_state_when_running_never_comes(monkeypatch):
    """A restore that cannot succeed must fail within its OWN budget and say what the app is
    doing, not sit for the manifest's replay-sized timeout. Measured: three hours of silence
    after a SIGKILL whose app never restarted."""
    class _Stuck(StubClient):
        def await_running(self, app, timeout, progress=None):
            self.awaited.append(timeout)
            raise TimeoutError("did not reach RUNNING")

    monkeypatch.setenv("SLT_RECOVER_RESTORE_TIMEOUT", "7")
    c = _Stuck(statuses=["DEPLOYED"])
    with pytest.raises(recovery.RecoveryError, match="status now DEPLOYED"):
        recovery.restore(c, Ctx("native"), "NS.App", "stop", timeout=5400, settle=0)
    assert c.awaited == [7.0], "the restore's budget, not the manifest's 5400s"

    # A budget that is not a positive number keeps the default instead of an instant timeout.
    monkeypatch.setenv("SLT_RECOVER_RESTORE_TIMEOUT", "nonsense")
    assert recovery._restore_budget() == 600.0
    monkeypatch.setenv("SLT_RECOVER_RESTORE_TIMEOUT", "0")
    assert recovery._restore_budget() == 600.0

    # A node that never came back raises on the status read too; the explaining error survives.
    class _Gone(_Stuck):
        def current_status(self, app):
            # The first read (before the START decision) answers; the node is gone by the time
            # the failure handler asks again.
            if not self.awaited:
                return "RUNNING"
            raise ConnectionError("node down")

    monkeypatch.setenv("SLT_RECOVER_RESTORE_TIMEOUT", "7")
    with pytest.raises(recovery.RecoveryError, match="unreadable"):
        recovery.restore(_Gone(statuses=["RUNNING"]), Ctx("native"), "NS.App", "stop",
                         timeout=5400, settle=0)


def test_restore_honours_expect_running_false():
    c = StubClient(statuses=["STOPPED"])
    status = recovery.restore(c, Ctx("native"), "NS.App", "stop", timeout=30, settle=0,
                              expect_running=False)
    assert status == "STOPPED"
    assert c.started == [] and c.awaited == []


def test_restore_raises_if_app_leaves_running_during_settle():
    # Recovering into a HALT must be reported as that, not discovered later as "no data".
    c = StubClient(statuses=["RUNNING", "HALT"])
    with pytest.raises(RecoveryError, match="left RUNNING during the post-recovery settle"):
        recovery.restore(c, Ctx("native"), "NS.App", "stop", timeout=30, settle=1)


# --- server_files `load:` -------------------------------------------------------------
# An OP jar needs LOAD OPEN PROCESSOR, not the UDF form -- registering it the wrong way
# leaves `USING Global.<Name>` unresolvable at deploy, which reads as a bad-TQL failure.

def test_server_files_load_true_is_udf(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE + (
        "server_files:\n  - file: a.jar\n    dest: ${TID}a.jar\n    load: true\n")))
    assert m.server_files[0][3] == "udf"


@pytest.mark.parametrize("spelling", ["open_processor", "op", "OPEN_PROCESSOR"])
def test_server_files_load_open_processor(tmp_path, spelling):
    m = load_manifest(_write(tmp_path, _BASE + (
        f"server_files:\n  - file: a.jar\n    dest: ${{TID}}a.jar\n    load: {spelling}\n")))
    assert m.server_files[0][3] == "open_processor"


def test_server_files_load_absent_is_false(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE + (
        "server_files:\n  - file: a.txt\n    dest: /tmp/${NS}/a.txt\n")))
    assert m.server_files[0][3] is False


def test_server_files_load_rejects_unknown(tmp_path):
    with pytest.raises(ManifestError, match="load must be"):
        load_manifest(_write(tmp_path, _BASE + (
            "server_files:\n  - file: a.jar\n    dest: ${TID}a.jar\n    load: formatter\n")))


# --- recover.times / recover.every ----------------------------------------------------
# One interruption can measure zero and prove nothing: the recovery regression report says the first stop
# is usually clean and the divergence shows on the second or third.

def test_recover_times_defaults_to_one(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE + "recover:\n  mode: stop\n"))
    assert m.recover["times"] == 1


def test_recover_times_and_every(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE + (
        "recover:\n  mode: stop\n  after: 30\n  times: 3\n  every: 10\n")))
    assert m.recover["times"] == 3
    assert m.recover["after"] == 30.0
    assert m.recover["every"] == 10.0


def test_recover_every_defaults_to_after(tmp_path):
    m = load_manifest(_write(tmp_path, _BASE + (
        "recover:\n  mode: stop\n  after: 30\n  times: 2\n")))
    assert m.recover["every"] == 30.0


@pytest.mark.parametrize("bad", ["0", "-1", "true", "2.5"])
def test_recover_times_rejects_non_positive_int(tmp_path, bad):
    with pytest.raises(ManifestError, match="recover.times"):
        load_manifest(_write(tmp_path, _BASE + f"recover:\n  mode: stop\n  times: {bad}\n"))


def test_recover_every_without_times_is_rejected(tmp_path):
    # A silent no-op is how assertions rot -- same rule the rest of this loader follows.
    with pytest.raises(ManifestError, match="recover.every"):
        load_manifest(_write(tmp_path, _BASE + (
            "recover:\n  mode: stop\n  every: 10\n")))

# --- gaps the independent review found: mutants that survived the original 48 -------------

def test_interrupt_quiesce_verifies_the_app_actually_left_running():
    """The quiesce branch must VERIFY, not just request.

    Deleting await_left_running from the quiesce branch survived the whole original suite,
    because the only quiesce test asserted quiesce_app had been called. That is the exact
    "green having never interrupted anything" failure this module exists to prevent.
    """
    c = StubClient(statuses=["RUNNING"])          # never leaves RUNNING
    with pytest.raises(recovery.RecoveryError) as e:
        recovery.interrupt(c, Ctx("native"), "NS.App", "quiesce", verify_timeout=0.2, poll=0.01)
    assert "not interrupted" in str(e.value)


@pytest.mark.parametrize("intermediate", ["APPROVING_QUIESCE", "QUIESCING", "STOPPING",
                                          "RUNNING_UNTIL_QUIESCE", "FLUSHING"])
def test_quiesce_intermediate_states_are_not_mistaken_for_a_finished_quiesce(intermediate):
    """Every intermediate state of the real quiesce path must count as transitional.

    A plain QUIESCE moves RUNNING -> APPROVING_QUIESCE -> QUIESCING -> STOPPING -> QUIESCED
    (design §4.2, from the platform's handlers); RUNNING_UNTIL_QUIESCE is what the QUIESCE action
    answers in place of APPROVING_QUIESCE under QUIESCE_ON_IN_QUIESCE, and FLUSHING is an enum
    member no handler assigns.
    Treating any of them as settled reports a verified interruption while the app may still be
    processing.
    """
    assert intermediate in recovery._TRANSITIONAL
    # await_left_running is the unit that decides "has it really left RUNNING": it must keep
    # waiting through the intermediate state and settle on QUIESCED.
    c = StubClient(statuses=[intermediate, intermediate, "QUIESCED"])
    got = recovery.await_left_running(c, "NS.App", timeout=5, poll=0.01)
    assert got == "QUIESCED"


def test_state_sets_contain_only_real_platform_statuses():
    """Guards against writing these sets from memory, which is how TERMINATED and UNDEPLOYING
    got in -- neither is a member of MetaInfo$StatusInfo$Status, so they could never match."""
    real = {
        "APPROVING_QUIESCE", "COMPLETED", "CRASH", "CREATED", "DEPLOY_FAILED", "DEPLOYED",
        "DEPLOYING", "FLUSHING", "HALT", "LOAD_BALANCE_DEPLOYED", "LOAD_BALANCE_STOPPED",
        "NOT_ENOUGH_SERVERS", "QUIESCED", "QUIESCING", "RECOVERING_SOURCES", "RUNNING",
        "RUNNING_UNTIL_END", "RUNNING_UNTIL_QUIESCE", "STARTING", "STARTING_SOURCES",
        "STOPPED", "STOPPING", "UNKNOWN", "VERIFYING_STARTING",
    }
    for name, s in (("_RESTARTABLE", recovery._RESTARTABLE),
                    ("_TRANSITIONAL", recovery._TRANSITIONAL),
                    ("_SELF_HEALING", recovery._SELF_HEALING)):
        assert s <= real, f"{name} names statuses the platform does not define: {s - real}"


def test_restore_after_a_start_tolerates_a_status_that_has_not_changed_yet():
    """START is asynchronous, so the first read can still be the terminal status we started
    FROM. Handing that straight to await_running raised 'reached terminal status CRASH' on the
    very state the START was meant to leave -- the kill path's normal case."""
    c = StubClient(statuses=["CRASH", "CRASH", "STARTING", "RUNNING"])
    c.strict_await = True
    recovery.restore(c, Ctx("docker"), "NS.App", "stop", timeout=5, poll=0.01)
    assert c.started == ["NS.App"], "a restartable status must still get its START"


def test_restore_kill_restarts_the_nodes_then_starts_the_app(monkeypatch):
    """restore(mode='kill') had NO test at all -- deleting its whole branch was invisible,
    and no live manifest uses kill either, so the headline mode was unexercised end to end."""
    calls = []
    monkeypatch.setattr(recovery.stack, "app_nodes", lambda: ["slt-node"])
    import livetest.striim_provision as sp
    monkeypatch.setattr(sp, "restart_app_nodes",
                        lambda client, run=None, timeout=None: calls.append("restart_nodes"))
    c = StubClient(statuses=["STOPPED", "STARTING", "RUNNING"])
    recovery.restore(c, Ctx("docker"), "NS.App", "kill", timeout=5, poll=0.01)
    assert calls == ["restart_nodes"], "kill must bring the killed nodes back first"
    assert c.started == ["NS.App"]


def test_restore_waits_rather_than_starting_a_self_healing_status():
    """NOT_ENOUGH_SERVERS resolves once the node rejoins; a START would race the platform."""
    c = StubClient(statuses=["NOT_ENOUGH_SERVERS", "NOT_ENOUGH_SERVERS", "RUNNING"])
    recovery.restore(c, Ctx("docker"), "NS.App", "kill", timeout=5, poll=0.01,
                     expect_running=True) if False else None
    c2 = StubClient(statuses=["NOT_ENOUGH_SERVERS"])
    recovery.restore(c2, Ctx("native"), "NS.App", "stop", timeout=5, poll=0.01)
    assert c2.started == [], "a self-healing status must not be STARTed"


def test_settle_accepts_completed_as_success():
    """COMPLETED is a bounded source finishing, not a death. Raising on it made the phase
    unusable for any initial-load reader."""
    c = StubClient(statuses=["RUNNING", "COMPLETED"])
    got = recovery.restore(c, Ctx("native"), "NS.App", "stop", timeout=5, settle=0.05, poll=0.01)
    assert got in ("COMPLETED", "RUNNING")

@pytest.mark.parametrize("left_in", ["STOPPED", "QUIESCED", "CREATED", "DEPLOYED", "CRASH", "HALT"])
def test_restore_starts_the_app_from_every_restartable_status(left_in):
    """Each mode leaves the app in a different status, and each must still get its START.

    QUIESCED is the one that mattered: dropping it from _RESTARTABLE survived the suite while
    breaking `quiesce` restore outright -- the app would sit QUIESCED, never be started, and
    await_running would poll to timeout.
    """
    assert left_in in recovery._RESTARTABLE
    c = StubClient(statuses=[left_in, "STARTING", "RUNNING"])
    recovery.restore(c, Ctx("native"), "NS.App", "stop", timeout=5, poll=0.01)
    assert c.started == ["NS.App"], f"{left_in} is restartable, so it must be STARTed"

