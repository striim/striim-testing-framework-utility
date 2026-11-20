from __future__ import annotations
import re
import time

from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result
from livetest.striim import TERMINAL_STATUSES, StriimTimeout


#: Where one server-log record starts: a line that begins with a timestamp. Every line up to the next
#: such line (a stack trace, a multi-line message) continues the record above it. Text before the
#: first timestamped line is a record of its own.
LOG_RECORD_START = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")

#: Joins the log text of two app nodes (plugin._node_log_since). A node's text starts at a byte mark
#: that can fall inside another app's multi-line record, so its first lines may be a continuation;
#: across this boundary they start a record of their own and never continue the other node's last
#: record. Whitespace only, so text made of empty slices still strips to "".
NODE_LOG_BOUNDARY = "\n\x1e\n"


def log_records(text: str) -> list[str]:
    """``text`` split into server-log records, each with its continuation lines (LOG_RECORD_START).
    A record never spans a NODE_LOG_BOUNDARY."""
    records = []
    for node_text in text.split(NODE_LOG_BOUNDARY):
        current = ""
        for line in node_text.splitlines(keepends=True):
            if LOG_RECORD_START.match(line) and current:
                records.append(current)
                current = ""
            current += line
        if current:
            records.append(current)
    return records


def _names(record: str, namespaces) -> bool:
    return any(re.search(rf"(?<![A-Za-z0-9_]){re.escape(ns)}(?![A-Za-z0-9_])", record) for ns in namespaces)


def assert_halt(client, app, timeout: int, poll: float = 2.0,
                *, db: str | None = None, progress=None,
                contains: tuple = (), log_tail=None, mode: str | None = None) -> list[dict]:
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
    # node server-log slice (plugin._halt_log_tail). `mode` is the Striim context's mode.
    # Only "native" has no node log to read: there we PASS but stamp "halt reason not checked
    # (native mode)" into the record detail so the gap is greppable, never silently skipped.
    # In any other mode the log exists, so a tail that raised or came back empty means the
    # reason was never checked, and that FAILS, with no record: a halt for the wrong reason must
    # not pass, and an infrastructure failure must not read as the documented defect.
    #
    # The slice is every app node's log since the case deployed, and other apps on the cluster
    # (sibling cases under SLT_PARALLEL) write to it too. So a substring counts only inside a log
    # record (LOG_RECORD_START) that names one of this case's namespaces; each substring may come
    # from a different such record.
    apps = [app] if isinstance(app, str) else list(app)
    namespaces = sorted({a.split(".", 1)[0] for a in apps})
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
                    except Exception as te:      # native: the caveat path; otherwise a failure below
                        tail, tail_err = "", te
                if not tail.strip() and mode != "native":
                    why = (f"the node log could not be read ({tail_err})" if tail_err is not None
                           else "no node-log reader was given" if log_tail is None
                           else "the node log has no text since the case deployed")
                    detail = (f"expect_halt_contains: terminal as expected ({shown}) but {why}, so "
                              f"{list(contains)!r} could not be checked ({mode or 'unknown'} mode)")
                    # Record-less on purpose, like the status-read error below: a log that could not
                    # be read is not the halt the manifest documents, and an xfail whose tiers
                    # include `halt` must not absorb it (ExpectedAssertionFailure.if_covered refuses
                    # an AssertionFailed with no records).
                    raise AssertionFailed(detail, [])
                if not tail.strip():
                    why = (f"node-log tail unavailable ({tail_err})" if tail_err is not None
                           else "no node-log tail on this deployment mode")
                    detail = (f"terminal as expected: {shown}; halt reason not checked "
                              f"(native mode): {why}, so expect_halt_contains "
                              f"{list(contains)!r} was NOT verified")
                    return [build_assertion_result(type="halt", status="passed", spec={},
                                                   target=None, db=db, detail=detail)]
                own = "".join(r for r in log_records(tail) if _names(r, namespaces))
                missing = [s for s in contains if s not in own]
                if missing:
                    elsewhere = [s for s in missing if s in tail]
                    detail = (f"expect_halt_contains: terminal as expected ({shown}) but the "
                              f"node log tail is missing {missing!r} (required, all of: "
                              f"{list(contains)!r})")
                    if elsewhere:
                        detail += (f"; {elsewhere!r} appear only in log records that do not name "
                                   f"{', '.join(namespaces)} (another app's output)")
                    raise AssertionFailed(detail, [build_assertion_result(
                        type="halt", status="failed", spec={}, target=None, db=db, detail=detail)])
                verified = [s for s in contains if s in own]
                detail = (f"terminal as expected: {shown}; expect_halt_contains verified: "
                          f"found {list(verified)!r} (all {len(contains)} required) in log "
                          f"records naming {', '.join(namespaces)}")
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


def skipped_by_startup_halt(m) -> list[str]:
    """What manifest ``m`` declares to run after the app starts -- post_start seeds, generate
    specs, server files and transaction steps, `lifecycle:`, and every `assert:` tier -- none of which runs once a
    deploy/startup failure is accepted as the expected halt."""
    out = [f"seed {f} (post_start)" for _db, f, when, _a in m.seed_files if when == "post_start"]
    out += [f"generate {g.get('kind')} (post_start)" for g in m.generate_specs
            if g.get("when") == "post_start"]
    out += [f"server_files {f} (post_start)" for f, _d, when, _l in m.server_files
            if when == "post_start"]
    out += [f"transaction {step['session']} {step['op']}"
            + (f" {step['file']}" if step.get("file") else "")
            + f" ({step['when']})"
            for step in m.transaction_steps if step["when"] != "pre_deploy"]
    if m.lifecycle is not None:
        out.append("lifecycle")
    out += [f"assert: {tier}" for tier in m.assert_]
    return out


def accept_expected_halt(expect_halt: bool, exc: Exception,
                         contains: tuple = (), skipped: tuple = ()) -> list[dict] | None:
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

    `skipped` is what the manifest declares to run after start (skipped_by_startup_halt).
    Accepting the halt drops all of it, so without `contains` this RAISES: an unpinned
    expect_halt would otherwise pass a case whose app never started for an unrelated reason,
    and its seeds and asserts would never run. With a matching `contains` the startup refusal
    is the declared outcome; the record names what was skipped.
    """
    # A list means ALL substrings must match (AND), not any-of: a compound halt reason
    # (table name + conflict type) is the case that needs the extra precision this key
    # exists to add, and AND fails closed -- adding a substring can only narrow what the
    # test accepts. An OR-style "phrasing varies across Striim versions" case is already
    # expressible by naming the one substring that is stable across versions.
    if not expect_halt or isinstance(exc, StriimTimeout):
        return None      # a timed-out import may still be running: never the expected outcome
    text = str(exc)
    if skipped and not contains:
        detail = (f"expect_halt: app failed during deploy/startup, so the manifest's "
                  f"{list(skipped)!r} would not run; set expect_halt_contains to the "
                  f"refusal text if a startup refusal is the expected outcome; "
                  f"actual failure text: {text}")
        raise AssertionFailed(detail, [build_assertion_result(
            type="halt", status="failed", spec={}, detail=detail)])
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
    if skipped:
        detail += f"; skipped (startup refusal): {list(skipped)!r}"
    return [build_assertion_result(type="halt", status="passed", spec={}, detail=detail)]
