"""The exit code a run reports: aggregation across tiers and runners, one pytest tier's mapping
(dispatch.map_exit), what the guard records, and the JUnit facts a runner verdict is built from."""
import sys
from types import SimpleNamespace

import pytest

from _clikit import REPO

sys.path.insert(0, str(REPO / "scripts" / "cli"))

from striim_test import dispatch, pytest_guard  # noqa: E402
from striim_test.errors import aggregate  # noqa: E402
from striim_test.junit import JunitError, parse_junit  # noqa: E402


@pytest.mark.parametrize("codes,expected", [
    ([1, 3], 1), ([3, 1], 1),               # a failure is never reported as a skip
    ([2, 1, 3], 2), ([4, 1, 3], 4), ([2, 4], 2),
    ([3], 3), ([3, 0], 3), ([1, 0, 5], 1),
    ([5, 5], 5), ([], 5), ([0, 5], 0), ([0], 0),
])
def test_aggregate_precedence(codes, expected):
    assert aggregate(codes) == expected


SEL = {"selected": [{"id": "live:a::a", "nodeid": "a/test.yaml::a"}]}


def _res(**kw):
    out = {"passed": [], "failed": [], "skipped": [], "xfailed": [], "errors": []}
    out.update(kw)
    return out


_A = [{"nodeid": "a/test.yaml::a"}]


@pytest.mark.parametrize("rc,sel,res,collect_only,expected", [
    (0, None, None, False, (2, "guard-selection-missing")),
    (1, None, None, False, (1, "tests-failed")),
    (2, None, None, False, (4, "interrupted")),
    (5, None, None, False, (5, "no-tests-collected")),
    (3, None, None, False, (2, "pytest-exit:3")),
    (0, dict(SEL, provenanceViolations=["x"]), _res(passed=_A), False, (2, "provenance-violation")),
    (0, SEL, _res(passed=_A, provenanceViolations=["x"]), False, (2, "provenance-violation")),
    (4, dict(SEL, duplicates={"a": ["x", "y"]}), None, False, (2, "duplicate-case")),
    (2, dict(SEL, collectionErrors=[{"nodeid": "b/test.yaml"}]), None, False, (2, "collection-error")),
    (5, {"selected": []}, None, False, (5, "no-tests-selected")),
    (0, SEL, None, True, (0, "collected")),
    (0, SEL, None, False, (2, "guard-results-missing")),
    (0, SEL, _res(passed=_A, collectOnly=True), False, (2, "selected-not-executed")),
    (0, SEL, _res(), False, (2, "selected-not-executed")),
    (0, SEL, _res(skipped=_A), False, (3, "selected-item-skipped")),
    (0, SEL, _res(xfailed=_A), False, (0, "ok")),
    (0, SEL, _res(passed=_A), False, (0, "ok")),
    (1, SEL, _res(skipped=_A), False, (3, "selected-item-skipped")),
    (1, SEL, _res(failed=_A), False, (1, "tests-failed")),
    (1, SEL, _res(errors=_A), False, (1, "tests-failed")),
    (1, SEL, _res(failed=_A, skipped=[{"nodeid": "b"}]), False, (1, "tests-failed")),
    (2, SEL, _res(), False, (4, "interrupted")),
    (5, SEL, None, False, (5, "no-tests-collected")),
    (4, SEL, None, False, (2, "pytest-exit:4")),
])
def test_map_exit(rc, sel, res, collect_only, expected):
    assert dispatch.map_exit(rc, sel, res, collect_only) == expected


@pytest.fixture
def guard_state(monkeypatch):
    state = {}
    monkeypatch.setattr(pytest_guard, "_STATE", state)
    monkeypatch.setenv(pytest_guard.ENV, "unused")
    state.update(cfg={}, collection_errors=[],
                 results={"passed": [], "failed": [], "skipped": [], "xfailed": [], "errors": []})
    return state


def _rep(when, outcome, longrepr=None, **kw):
    return SimpleNamespace(nodeid="a/test.yaml::a", when=when, outcome=outcome, longrepr=longrepr, **kw)


def test_guard_records_xfail_skip_and_setup_teardown_errors(guard_state):
    for rep in (_rep("call", "skipped", wasxfail="known bug"),
                _rep("setup", "skipped", ("f", 1, "Skipped: no cluster")),
                _rep("setup", "failed", "boom in setup"),
                _rep("teardown", "failed", "boom in teardown"),
                _rep("call", "failed", "assert 1 == 2")):
        pytest_guard.pytest_runtest_logreport(rep)
    r = guard_state["results"]
    assert r["xfailed"] == [{"nodeid": "a/test.yaml::a"}]
    assert r["skipped"] == [{"nodeid": "a/test.yaml::a", "reason": "Skipped: no cluster"}]
    assert [(e["when"], e["message"]) for e in r["errors"]] == [("setup", "boom in setup"),
                                                                ("teardown", "boom in teardown")]
    assert r["failed"] == [{"nodeid": "a/test.yaml::a", "message": "assert 1 == 2"}]


def test_guard_records_collection_errors(guard_state):
    pytest_guard.pytest_collectreport(SimpleNamespace(failed=True, nodeid="b/test.yaml",
                                                      longrepr="ManifestError: bad"))
    pytest_guard.pytest_collectreport(SimpleNamespace(failed=False, nodeid="c/test.yaml", longrepr=None))
    assert guard_state["collection_errors"] == [{"nodeid": "b/test.yaml", "detail": "ManifestError: bad"}]


def _xml(tmp_path, body):
    p = tmp_path / "junit.xml"
    p.write_text(body)
    return p


def test_junit_error_testcase_is_counted_as_failing(tmp_path):
    facts = parse_junit(_xml(tmp_path, '<testsuite tests="2"><testcase classname="c" name="a"/>'
                                       '<testcase classname="c" name="b"><error message="x"/></testcase>'
                                       '</testsuite>'))
    assert (facts.tests, facts.failures, facts.errors, facts.failing) == (2, 0, 1, ["c::b"])


@pytest.mark.parametrize("body,match", [
    ('<testsuite tests="3"><testcase classname="c" name="a"/></testsuite>', "declares 3 tests but contains 1"),
    ('<testsuites><testsuite tests="1"><testcase classname="c" name="a"/></testsuite>'
     '<testsuite tests="2"><testcase classname="c" name="b"/></testsuite></testsuites>',
     "declares 3 tests but contains 2"),
    ('<testsuite tests="many"><testcase classname="c" name="a"/></testsuite>', "non-integer tests"),
])
def test_junit_truncated_or_malformed_count_refused(tmp_path, body, match):
    with pytest.raises(JunitError, match=match) as e:
        parse_junit(_xml(tmp_path, body))
    assert e.value.reason == "report-malformed"


def test_unknown_assert_tier_is_a_test_yaml_mistake(tmp_path):
    # doctor refuses it as it refuses an unknown top-level key; a run fails the case instead of
    # passing on smoke alone.
    case = tmp_path / "bad-tier"
    case.mkdir()
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\n")
    (case / "test.yaml").write_text("name: bad-tier\ntql: app.tql\nrequires: [postgres]\n"
                                    "assert:\n  smoke: true\n  dataa: []\n")
    from striim_test import doctor
    [check] = doctor.check_manifests([case / "test.yaml"])
    assert (check.status, check.code) == (doctor.FAIL, 2) and "unknown tier(s) ['dataa']" in check.message
    assert aggregate([check.code, 3]) == 2                 # beside an unreachable Striim, still 2
