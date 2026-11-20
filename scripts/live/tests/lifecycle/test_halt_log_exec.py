"""`expect_halt_contains` on a polled halt, through the real plugin: in docker mode a node log that
could not be read fails the case, even under an xfail; only native mode passes with "halt reason
not checked".

The `legacy` fixture with `expect_halt` in place of `smoke`, an app that is CRASH once started,
and the executed-plugin harness, whose node-log marks are empty (no `docker exec` reaches a node).
"""
from .exec_harness import run_case  # noqa: F401

pytest_plugins = ["pytester"]

CRASHED = '''
FakeClient.current_status = lambda self, app: "CRASH" if STATE["running"] else "CREATED"
'''
# The reason counts only in a log record that names the case's namespace, so the log line names
# every namespace whose status was read (the tokenized one is not known before the run).
LOG_HAS_REASON = '''
_namespaces = set()
_crashed = FakeClient.current_status
def _status(self, app):
    _namespaces.add(app.split(".", 1)[0])
    return _crashed(self, app)
FakeClient.current_status = _status
P._node_log_since = lambda ctx, marks, run=None, **kw: "".join(
    f"2026-11-14 10:00:00,000 ERROR {ns}.app ORA-00001: unique constraint violated\\n" for ns in sorted(_namespaces))
'''
NATIVE = '''
_resolve_striim = P._resolve_striim
def _native(config):
    ctx = _resolve_striim(config)
    ctx.mode = "native"
    return ctx
P._resolve_striim = _native
'''


def _halt_case(text):
    assert text.rstrip().endswith("assert:\n  smoke: true"), text
    return text.rstrip()[:-len("assert:\n  smoke: true")] + "expect_halt: true\nexpect_halt_contains: ORA-00001\n"


def _prepare(*extra):
    def prepare(root, name):
        with (root / "conftest.py").open("a") as f:
            f.write("\n" + CRASHED + "".join(extra))
    return prepare


def _halt_record(run):
    halts = [a for a in run.sidecar()["assertions"] if a["type"] == "halt"]
    assert len(halts) == 1, run.sidecar()["assertions"]
    return halts[0]


def _unreadable_failure(run):
    assert run.ret == 1, run.text[-3000:]
    status, _props, xml = run.junit()
    assert status == "failed", xml
    assert "node log could not be read" in xml and "no start mark" in xml, xml
    # raised without a record, so no xfail covering `halt` can absorb it
    assert [a for a in run.sidecar()["assertions"] if a["type"] == "halt"] == []


def test_docker_mode_fails_a_halt_whose_node_log_could_not_be_read(run_case):
    _unreadable_failure(run_case("legacy", edit=_halt_case, prepare=_prepare()))


def test_a_strict_xfail_does_not_absorb_an_unreadable_node_log(run_case):
    """The default xfail tiers include `halt`; a log that could not be read is not the defect."""
    edit = lambda y: _halt_case(y) + "xfail: {reason: known defect, strict: true}\n"  # noqa: E731
    _unreadable_failure(run_case("legacy", edit=edit, prepare=_prepare()))


def test_docker_mode_passes_a_halt_whose_reason_is_in_the_log(run_case):
    run = run_case("legacy", edit=_halt_case, prepare=_prepare(LOG_HAS_REASON))
    assert run.ret == 0, run.text[-3000:]
    rec = _halt_record(run)
    assert rec["status"] == "passed" and "verified" in rec["detail"] and "NOT verified" not in rec["detail"]


def test_native_mode_keeps_the_unverified_pass(run_case):
    run = run_case("legacy", edit=_halt_case, prepare=_prepare(NATIVE))
    assert run.ret == 0, run.text[-3000:]
    rec = _halt_record(run)
    assert rec["status"] == "passed" and "halt reason not checked (native mode)" in rec["detail"], rec["detail"]
