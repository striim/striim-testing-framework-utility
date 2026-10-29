import pytest
from livetest import progress
from livetest.assertions import AssertionFailed, ExpectedAssertionFailure
class Clock:
    def __init__(self): self.t = 0
    def now(self): return self.t
    def sleep(self, seconds): self.t += seconds


def spec():
    return dict(kind="target-count", db="postgres-target", table="qatarget.rows",
                above=2, below=10, timeout="1s", poll="100ms")


def test_condition_rechecked_immediately_before_interruption():
    clock = Clock(); values = iter([3, 4, 5]); events = []; records = []
    def read(left): events.append("read"); return next(values)
    progress.at_fault(progress.normalize(spec()), read, lambda: events.append("interrupt"),
        record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    assert events == ["read", "read", "interrupt", "read"]
    actual = records[0]["actual"]["rows"][0]
    assert actual["value"] == 4 and actual["held"] is True
    assert actual["issuedAt"] == actual["observedAt"] == 0
    assert records[0]["type"] == "recovery" and records[0]["status"] == "passed"


def test_window_closed_between_wait_and_fault_fails_with_record():
    clock = Clock(); values = iter([3, 10]); records = []
    with pytest.raises(progress.ProgressError, match="fault-missed") as caught:
        progress.at_fault(progress.normalize(spec()), lambda left: next(values),
            lambda: pytest.fail("missed fault issued"), record=records.append,
            cycle=2, now=clock.now, sleep=clock.sleep)
    assert not isinstance(caught.value, AssertionFailed)
    actual = records[0]["actual"]["rows"][0]
    assert actual["value"] == 10 and actual["held"] is False and actual["issuedAt"] is None
    assert actual["cycle"] == 2 and records[0]["status"] == "failed"


def test_deadline_records_last_observation_and_never_xfails():
    clock = Clock(); records = []
    with pytest.raises(progress.ProgressError, match="fault-missed") as caught:
        progress.at_fault(progress.normalize(spec()), lambda left: 0,
            lambda: pytest.fail("fault without progress"), record=records.append,
            cycle=1, now=clock.now, sleep=clock.sleep)
    assert not isinstance(caught.value, ExpectedAssertionFailure)
    assert records[0]["actual"]["rows"][0]["value"] == 0
    assert records[0]["actual"]["rows"][0]["observedAt"] < 1


def test_interrupt_failure_cannot_be_recorded_as_passed():
    clock = Clock(); records = []
    def fail(): raise RuntimeError("command rejected")
    with pytest.raises(RuntimeError, match="rejected"):
        progress.at_fault(progress.normalize(spec()), lambda left: 4, fail,
            record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    assert records[0]["status"] == "failed"
    assert records[0]["actual"]["rows"][0]["held"] is True
    assert records[0]["actual"]["rows"][0]["issuedAt"] == 0


def test_record_schema_accepts_recovery_fault_record():
    clock = Clock(); records = []
    progress.at_fault(progress.normalize(spec()), lambda left: 4, lambda: None,
        record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    assert records[0]["type"] == "recovery"
    from livetest.resultschema import _validate_assertion
    _validate_assertion(records[0], "fault")


def test_stop_that_drains_the_last_batch_fails_and_retains_both_boundaries():
    clock = Clock(); values = iter([3, 4, 10]); records = []
    with pytest.raises(progress.ProgressError, match='fault-missed'):
        progress.at_fault(progress.normalize(spec()), lambda left: next(values), lambda: None,
            record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    actual = records[0]['actual']['rows'][0]
    assert records[0]['status'] == 'failed' and actual['interrupted'] is True
    assert actual['windowOpened']['value'] == 3
    assert actual['value'] == 4 and actual['afterInterrupt']['value'] == 10


def test_success_retains_the_first_window_and_post_interrupt_observation():
    clock = Clock(); values = iter([3,4,5]); records = []
    progress.at_fault(progress.normalize(spec()), lambda left: next(values), lambda: None,
        record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    actual = records[0]['actual']['rows'][0]
    assert actual['windowOpened']['value'] == 3 and actual['afterInterrupt']['value'] == 5


def test_owner_check_runs_once_after_window_opens_then_progress_is_rechecked():
    clock = Clock(); events = []; records = []; values = iter([0,3,4,5])
    def read(left): events.append('read'); return next(values)
    def check(left): events.append('owner'); clock.t += .05
    progress.at_fault(progress.normalize(spec()), read, lambda: events.append('interrupt'),
        record=records.append, cycle=1, now=clock.now, sleep=clock.sleep, before_issue=check)
    assert events == ['read','read','owner','read','interrupt','read']
    actual = records[0]['actual']['rows'][0]
    assert actual['windowOpened']['value'] == 3 and actual['value'] == 4


def test_transaction_observation_runs_once_at_boundary_not_during_progress_wait():
    import ast
    import inspect
    import textwrap
    from types import SimpleNamespace
    from livetest import plugin
    tree = ast.parse(textwrap.dedent(inspect.getsource(plugin.LiveItem._runtest)))
    callbacks = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                 and node.name in {"_read_at_fault", "_before_fault"}]
    assert len(callbacks) == 2
    clock = Clock(); events = []; values = iter([0, 0, 3, 4, 5]); records = []
    def observe(phase, remaining):
        events.append(phase)
        clock.t += .05
        assert 0 < remaining <= 2
    namespace = dict(time=SimpleNamespace(monotonic=clock.now),
        _tx_pool=SimpleNamespace(observe_recovery=observe),
        _fault_target=SimpleNamespace(check_owner=lambda left: events.append("owner")),
        _fault_progress=progress, app="N.app", _rc_app="N.child",
        # recover.app: the pre-fault status read is of the interrupted application, not ${APP}.
        client=SimpleNamespace(current_status=lambda name, **kw: "RUNNING" if name == "N.child" else "STOPPED"),
        _progress_read=lambda left: next(values))
    exec(compile(ast.Module(body=callbacks, type_ignores=[]), "callbacks", "exec"), namespace)
    progress.at_fault(progress.normalize(spec()), namespace["_read_at_fault"],
        lambda: events.append("interrupt"), before_issue=namespace["_before_fault"],
        after_read=namespace["_progress_read"], record=records.append, cycle=1,
        now=clock.now, sleep=clock.sleep)
    assert events == ["pre_interrupt", "owner", "interrupt"]
    assert records[-1]["status"] == "passed"
