from __future__ import annotations
import time

from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result
from livetest.striim import TERMINAL_STATUSES, StriimTimeout


def assert_halt(client, app, timeout: int, poll: float = 2.0,
                *, db: str | None = None, progress=None,
                contains: tuple = (), log_tail=None) -> list[dict]:
    # The `expect_halt` assertion: the test's CORRECT outcome is the app entering a
    # terminal status (HALT/CRASH/TERMINATED/DEPLOY_FAILED) -- e.g. a product raise-path
    # where a bad/No-Op event fails the batch. This is the inverse of assert_smoke: here a
    # terminal status is the PASS and a sustained non-terminal status is the FAIL.
    #
    # Poll every app until all are terminal (pass). If any stays non-terminal past the
    # deadline, fail with "expected HALT, still <status>". Either way we return/raise a
    # single record so the plugin always records the tier. Called AFTER the post_start seed
    # (the seed is what triggers the halt), so we don't pre-check for RUNNING here.
    #
    # `contains` (expect_halt_contains) narrows the pass condition from "terminal for any
    # reason" to "terminal AND the reason names these substrings". current_status gives no
    # error text, so the reason comes from `log_tail()` -- a zero-arg callable returning the
    # node server-log tail (plugin._node_log_tail). That returns "" on native single-node,
    # where the check is impossible: we PASS but stamp "halt reason not checked (native
    # mode)" into the record detail so the gap is greppable, never silently skipped.
    apps = [app] if isinstance(app, str) else list(app)
    started = time.monotonic()
    deadline = started + timeout
    try:
        while True:
            statuses = {a: client.current_status(a) for a in apps}
            pending = {a: s for a, s in statuses.items() if s not in TERMINAL_STATUSES}
            if not pending:
                shown = ", ".join(f"{a}={s}" for a, s in statuses.items())
                if not contains:
                    detail = "terminal as expected: " + shown
                    return [build_assertion_result(type="halt", status="passed", spec={},
                                                   target=None, db=db, detail=detail)]
                tail, tail_err = "", None
                if log_tail is not None:
                    try:
                        tail = log_tail() or ""
                    except Exception as te:      # degrades to the caveat path, not a false failure
                        tail, tail_err = "", te
                if not tail.strip():
                    why = (f"node-log tail unavailable ({tail_err})" if tail_err is not None
                           else "no node-log tail on this deployment mode")
                    detail = (f"terminal as expected: {shown}; halt reason not checked "
                              f"(native mode): {why}, so expect_halt_contains "
                              f"{list(contains)!r} was NOT verified")
                    return [build_assertion_result(type="halt", status="passed", spec={},
                                                   target=None, db=db, detail=detail)]
                missing = [s for s in contains if s not in tail]
                if missing:
                    detail = (f"expect_halt_contains: terminal as expected ({shown}) but the "
                              f"node log tail is missing {missing!r} (required, all of: "
                              f"{list(contains)!r})")
                    raise AssertionFailed(detail, [build_assertion_result(
                        type="halt", status="failed", spec={}, target=None, db=db, detail=detail)])
                verified = [s for s in contains if s in tail]
                detail = (f"terminal as expected: {shown}; expect_halt_contains verified: "
                          f"found {list(verified)!r} (all {len(contains)} required)")
                return [build_assertion_result(type="halt", status="passed", spec={},
                                               target=None, db=db, detail=detail)]
            if time.monotonic() >= deadline:
                detail = (f"expected a terminal HALT within {timeout}s; still non-terminal: "
                          + ", ".join(f"{a}={s}" for a, s in pending.items()))
                raise AssertionFailed(detail, [build_assertion_result(
                    type="halt", status="failed", spec={}, target=None, db=db, detail=detail)])
            if progress:
                try:
                    progress(time.monotonic() - started, float(timeout),
                             ", ".join(f"{a}={s}" for a, s in pending.items()))
                except Exception:
                    pass
            if poll:
                time.sleep(poll)
    except AssertionFailed:
        raise
    except Exception as e:
        # Record-less on purpose: an unreachable REST endpoint is not the halt the manifest
        # expects, and an xfail'd manifest must not read it as the documented defect --
        # ExpectedAssertionFailure.if_covered refuses an AssertionFailed with no records.
        detail = f"expect_halt: error reading application status: {e}"
        raise AssertionFailed(detail, []) from e


def accept_expected_halt(expect_halt: bool, exc: Exception,
                         contains: tuple = ()) -> list[dict] | None:
    """Called at a genuine (non-poison) deploy/startup failure site. Returns a passed
    halt record if `expect_halt` accepts this exception as the test's expected outcome;
    returns None if it doesn't (caller must re-raise the original exception unchanged --
    a normal test's deploy/startup failure stays fatal).

    `contains` is `expect_halt_contains`: a tuple of substrings that must ALL appear in
    the failure text. On a mismatch this RAISES AssertionFailed rather than returning a
    "failed" record -- the two call sites in plugin.py branch only on `is None` vs
    `not None`, and `_slt_records` is a reporting sidecar that is never scanned for
    `status == "failed"`, so a returned failed record would silently PASS a halt for the
    wrong reason. Raise-on-failure / return-on-success is also what every other assertion
    helper in this package does, including assert_halt above.
    """
    # A list means ALL substrings must match (AND), not any-of: a compound halt reason
    # (table name + conflict type) is the case that needs the extra precision this key
    # exists to add, and AND fails closed -- adding a substring can only narrow what the
    # test accepts. An OR-style "phrasing varies across Striim versions" case is already
    # expressible by naming the one substring that is stable across versions.
    if not expect_halt or isinstance(exc, StriimTimeout):
        return None      # a timed-out import may still be running: never the expected outcome
    text = str(exc)
    missing = [s for s in contains if s not in text]
    if missing:
        detail = (f"expect_halt_contains: app failed during deploy/startup as expected, but "
                  f"the failure text is missing {missing!r} (required, all of: "
                  f"{list(contains)!r}); actual failure text: {text}")
        raise AssertionFailed(detail, [build_assertion_result(
            type="halt", status="failed", spec={}, detail=detail)])
    detail = f"expect_halt: app failed during deploy/startup as expected: {exc}"
    if contains:
        verified = [s for s in contains if s in text]
        detail += f"; expect_halt_contains verified: found {list(verified)!r} (all {len(contains)} required)"
    return [build_assertion_result(type="halt", status="passed", spec={}, detail=detail)]
