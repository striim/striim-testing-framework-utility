"""One status read after the last assertion tier: an app that leaves RUNNING after its assertions passed,
or after a smoke-only case's settle, fails the case. Under `expect_halt` the halt is the expected end state
and the read is skipped. Executed through the real plugin with fakes at the edges (exec_harness)."""
from __future__ import annotations

from .exec_harness import CONFTEST, run_case  # noqa: F401 - run_case is a fixture

pytest_plugins = ["pytester"]

# The app is RUNNING through the smoke settle and crashes the moment the assertion has passed.
_CRASH_AFTER = '''

_slt_status = FakeClient.current_status
def _slt_crashing_status(self, app):
    log("status", value="CRASH" if STATE.get("crashed") else "live")
    return "CRASH" if STATE.get("crashed") else _slt_status(self, app)
FakeClient.current_status = _slt_crashing_status
_slt_smoke = P.assert_smoke
def _slt_smoke_then_crash(*a, **k):
    records = _slt_smoke(*a, **k)
    STATE["crashed"] = True
    log("smoke_passed")
    return records
P.assert_smoke = _slt_smoke_then_crash
'''


def _crash_after(root, name):
    conftest = root / "conftest.py"
    conftest.write_text(conftest.read_text() + _CRASH_AFTER)


def test_exec_a_smoke_only_case_fails_when_the_app_crashes_after_the_settle(run_case):
    run = run_case("legacy", prepare=_crash_after)
    assert "smoke_passed" in run.names(), run.text
    assert run.ret == 1, run.text
    assert run.junit()[0] == "failed"
    assert "entered terminal status 'CRASH' after the last assertion" in run.text
    # the read comes after the passing tier
    names = run.names()
    assert any(e["event"] == "status" and e["value"] == "CRASH" for e in run.events()[names.index("smoke_passed"):])


def test_exec_a_smoke_only_case_that_stays_running_passes(run_case):
    run = run_case("legacy")
    assert run.ret == 0, run.text


def test_exec_the_final_read_is_skipped_under_expect_halt(run_case):
    # The expected halt ends the app; reading its status again must not fail the case.
    halted = '''

_slt_status = FakeClient.current_status
def _slt_halted_status(self, app):
    if STATE.get("halted"):
        log("status", value="HALT")
        return "HALT"
    return _slt_status(self, app)
FakeClient.current_status = _slt_halted_status
def _slt_halt(client, apps, **k):
    STATE["halted"] = True
    log("halt_asserted")
    from livetest.resultschema import build_assertion_result
    return [build_assertion_result(type="halt", status="passed", spec={}, target=None, db=None, detail="HALT")]
P.assert_halt = _slt_halt
'''

    def prepare(root, name):
        conftest = root / "conftest.py"
        conftest.write_text(conftest.read_text() + halted)
    run = run_case("legacy", prepare=prepare,
                   edit=lambda y: y.replace("assert:\n  smoke: true\n", "expect_halt: true\n"))
    assert "halt_asserted" in run.names(), run.text
    assert run.ret == 0, run.text
    assert "status" not in run.names()[run.names().index("halt_asserted"):]     # no read after the halt
