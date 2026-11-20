"""The two xfail outcomes in evidence. An XPASS is a pass the manifest said would fail: every rule that
demotes a pass (a failed cleanup, an evidence error, a failed infrastructure teardown) demotes it too, and
it never qualifies. An xfailed case keeps its own status and its reason."""
from __future__ import annotations

from types import SimpleNamespace

from livetest import evidence

LIFECYCLE = {"mode": "initial-load", "ready": {"reason": "satisfied"}, "completion": {"reason": "satisfied"}}
CLEAN = {"status": "ok", "detail": None}
VERIFIED = {"cleanupVerified": True, "verificationGaps": []}


def test_report_status_maps_the_two_xfail_outcomes():
    xfailed = SimpleNamespace(when="call", outcome="skipped", wasxfail="known bug")
    xpassed = SimpleNamespace(when="call", outcome="passed", wasxfail="known bug")
    assert evidence._report_status(xfailed) == "xfailed"
    assert evidence._report_status(xpassed) == "xpassed"
    assert evidence._report_status(SimpleNamespace(when="call", outcome="skipped")) == "skipped"
    assert evidence._report_status(SimpleNamespace(when="call", outcome="passed")) == "passed"


def test_an_xpass_never_qualifies():
    assert evidence.final_outcome("passed", LIFECYCLE, CLEAN, VERIFIED) == ("passed", True, None)
    assert evidence.final_outcome("xpassed", LIFECYCLE, CLEAN, VERIFIED) == ("xpassed", False, "run status xpassed")


def test_a_failed_cleanup_fails_an_xpass_as_it_fails_a_pass():
    failed = {"status": "failed", "detail": "drop refused"}
    assert evidence.final_outcome("passed", LIFECYCLE, failed, VERIFIED)[0] == "failed"
    assert evidence.final_outcome("xpassed", LIFECYCLE, failed, VERIFIED)[0] == "failed"
    assert evidence.final_outcome("xfailed", LIFECYCLE, failed, VERIFIED)[0] == "xfailed"


def _session(results, **config):
    cfg = SimpleNamespace(_slt_results=results, pluginmanager=SimpleNamespace(get_plugins=lambda: []), **config)
    return SimpleNamespace(config=cfg, exitstatus=0)


def test_an_evidence_error_makes_an_xpass_an_error():
    results = {"n1": {"name": "a", "nodeid": "n1", "status": "xpassed"},
               "n2": {"name": "b", "nodeid": "n2", "status": "xfailed"}}
    s = _session(results, _slt_evidence_errors=[{"nodeid": "n1", "name": "a", "error": "x"},
                                                {"nodeid": "n2", "name": "b", "error": "y"}])
    evidence.fold_evidence_errors(s)
    assert results["n1"]["status"] == "error" and results["n2"]["status"] == "xfailed" and s.exitstatus == 1


def test_a_failed_teardown_makes_an_xpass_an_error():
    results = {"n1": {"name": "a", "nodeid": "n1", "status": "xpassed"}}
    s = _session(results, _slt_teardown_failures=[{"resource": "stack", "error": "down failed"}])
    evidence.finalize_session(s)
    assert results["n1"]["status"] == "error" and s.exitstatus == 1


def test_the_agreement_check_compares_xfail_outcomes_as_junit_records_them():
    # JUnit and the guard's results record an xfail as skipped and an XPASS as passed.
    assert evidence._as_junit("xfailed") == "skipped" and evidence._as_junit("xpassed") == "passed"
    assert all(evidence._as_junit(s) == s for s in ("passed", "failed", "skipped", "error", None))
