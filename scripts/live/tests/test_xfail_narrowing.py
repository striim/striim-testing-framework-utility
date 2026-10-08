"""ExpectedAssertionFailure.if_covered -- the S3i narrowing's decision, hermetically.

The `pytest_runtest_call` hookwrapper (plugin.py) re-types an AssertionFailed as ExpectedAssertionFailure only when
every FAILED record came from a tier the manifest's `xfail.tiers` names; the xfail marker is
`raises=ExpectedAssertionFailure`, so an untouched failure is reported as a real one.
"""
import pytest

from livetest.assertions import AssertionFailed, ExpectedAssertionFailure


def _rec(type_, status="failed"):
    return {"type": type_, "status": status, "spec": {}}


def test_a_failure_on_a_named_tier_is_expected():
    e = AssertionFailed("diff not satisfied", [_rec("diff")])
    out = ExpectedAssertionFailure.if_covered(e, ["diff"])
    assert isinstance(out, ExpectedAssertionFailure)
    assert str(out) == "diff not satisfied", "the detail survives the re-typing"
    assert out.records == e.records, "the sidecar still gets the records"
    assert out.__cause__ is e


def test_a_failure_on_an_unnamed_tier_is_not():
    e = AssertionFailed("data not satisfied", [_rec("data")])
    assert ExpectedAssertionFailure.if_covered(e, ["diff"]) is e


def test_mixed_tiers_are_expected_only_if_every_failed_one_is_named():
    e = AssertionFailed("x", [_rec("diff"), _rec("data")])
    assert ExpectedAssertionFailure.if_covered(e, ["diff"]) is e
    assert isinstance(ExpectedAssertionFailure.if_covered(e, ["diff", "data"]), ExpectedAssertionFailure)


def test_passed_records_do_not_count():
    # Four diffs, three passed, one failed on a named tier: expected.
    e = AssertionFailed("x", [_rec("diff", "passed")] * 3 + [_rec("diff")])
    assert isinstance(ExpectedAssertionFailure.if_covered(e, ["diff"]), ExpectedAssertionFailure)


def test_no_records_is_never_expected():
    # An AssertionFailed with no records is a spec problem (e.g. monitor 'target:' ambiguity),
    # not the documented defect.
    e = AssertionFailed("monitor: 2 targets, name one", [])
    assert ExpectedAssertionFailure.if_covered(e, ["monitor"]) is e
    e2 = AssertionFailed("x", None)
    assert ExpectedAssertionFailure.if_covered(e2, ["monitor"]) is e2


def test_smoke_is_never_covered_because_the_loader_never_names_it():
    # The loader rejects `tiers: [smoke]`; here the type check alone must still refuse when the
    # caller passes a tiers list that lacks it.
    e = AssertionFailed("app not RUNNING", [_rec("smoke")])
    assert ExpectedAssertionFailure.if_covered(e, ["data", "diff", "file", "gcs", "json", "monitor", "halt"]) is e


def test_expected_is_still_an_assertion_failed():
    # So every `except AssertionFailed` sidecar handler and pytest's AssertionError reporting
    # keep working unchanged.
    e = ExpectedAssertionFailure.if_covered(AssertionFailed("x", [_rec("data")]), ["data"])
    assert isinstance(e, AssertionFailed) and isinstance(e, AssertionError)


# ---- _active_xfail: xfail.releases resolved against this run's release -------------------------

class _Cfg:
    def __init__(self, version=None, exc=None):
        self._version, self._exc = version, exc

def _manifest(xfail):
    from types import SimpleNamespace
    return SimpleNamespace(xfail=xfail)

def _active(monkeypatch, cfg, xfail):
    from livetest import plugin
    def resolve(config):
        if config._exc:
            raise config._exc
        return {"STRIIM_VERSION": config._version}
    monkeypatch.setattr(plugin, "_resolve_release", resolve)
    return plugin._active_xfail(cfg, _manifest(xfail))

_XF = {"reason": "r", "strict": True, "tiers": ["json"], "releases": ["5.4.0.6-5.4.0.6F"]}

def test_active_xfail_applies_on_a_listed_release(monkeypatch):
    assert _active(monkeypatch, _Cfg("5.4.0.6C"), _XF) is _XF

def test_active_xfail_is_off_on_an_unlisted_release(monkeypatch):
    assert _active(monkeypatch, _Cfg("5.4.0.6G"), _XF) == {}

def test_active_xfail_never_resolves_without_releases(monkeypatch):
    xf = {"reason": "r", "strict": False, "tiers": ["json"]}
    assert _active(monkeypatch, _Cfg(exc=AssertionError("resolved")), xf) is xf

@pytest.mark.parametrize("cfg", [_Cfg(exc=None), _Cfg("5.4.2-SNAPSHOT")])
def test_active_xfail_does_not_abort_collection_on_a_bad_release(monkeypatch, cfg):
    # One manifest's release list must not fail the whole session: a missing/ambiguous install or
    # an unparseable version turns the xfail off (unlisted => must pass).
    from livetest.releases import ReleaseError
    if cfg._version is None:
        cfg._exc = ReleaseError("no Platform-*.jar under /x/lib")
    assert _active(monkeypatch, cfg, _XF) == {}
