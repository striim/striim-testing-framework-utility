from __future__ import annotations


class AssertionFailed(AssertionError):
    """Raised by an assert_* function when its assertion fails. Carries the structured
    per-spec records built so far so the plugin can still write them to the sidecar,
    while remaining an AssertionError so pytest/JUnit report it as a failure unchanged."""
    def __init__(self, detail, records):
        super().__init__(detail)
        self.records = records  # list[dict], each from resultschema.build_assertion_result


class ExpectedAssertionFailure(AssertionFailed):
    """An AssertionFailed that a manifest's `xfail.tiers` covers: every failed record came from a
    tier the manifest named. The `pytest_runtest_call` hookwrapper in plugin.py re-raises a covered failure as this type, and the xfail
    marker is `raises=ExpectedAssertionFailure`, so anything else that fails the test -- a deploy
    error, a provisioning timeout, a smoke failure, an assertion on a tier the manifest did not
    name, an AssertionFailed with no records (a spec problem) -- is reported as a FAILURE rather
    than absorbed as the documented defect (design §10.1)."""

    @classmethod
    def if_covered(cls, failure: "AssertionFailed", tiers) -> "AssertionFailed":
        """Return `failure` re-typed as expected when `tiers` covers it, else `failure` unchanged."""
        failed = [r for r in (failure.records or []) if r.get("status") == "failed"]
        if not failed:
            return failure
        if all(r.get("type") in set(tiers) for r in failed):
            wrapped = cls(str(failure), failure.records)
            wrapped.__cause__ = failure
            return wrapped
        return failure
